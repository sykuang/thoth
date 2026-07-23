"""Classify credit-card transactions into typed buckets.

Phase 6 (B-full): augment every billed/pending row with a `txn_type` enum
so the statistics layer can distinguish "this is real spending" from
"this is the bank refunding me / paying me cashback". Without this, raw
negative amounts coming back from the bank (e.g. ubot writes cashback
as a negative ledger entry) would be counted as expense and inflate the
monthly spending stat.

Per-bank persist callers SHOULD:

1.  Try the bank's own short-circuit field first (most reliable).
    - ubot:  `txCode in ('20','40','43','60')`
    - ctbc:  `txCode in ('20','40','60')`
    - hsbc:  `isPositive` bool + desc keyword
2.  Fall back to `classify_by_desc_and_sign(desc, amount_signed)` for
    banks without an enum field (cathay/sinopac/taishin/esun).

The fallback is intentionally keyword-driven; banks invent new merchant
descriptions all the time, so an explicit unknown bucket is preferable
to misclassifying.
"""
from __future__ import annotations

# --- txn_type 枚舉 (string 常數，方便直接寫進 SQLite TEXT 欄) ---
SPENDING = "spending"        # 一般消費 → 進 expense
CASHBACK = "cashback"        # 刷卡金/現金回饋/活動贈金 → 進 income, 即使 amount<0 也算正向
REFUND = "refund"            # 商家退款 → 進 income
PAYMENT = "payment"          # 還錢給銀行/自動扣繳 → 既不入 expense 也不入 income (是還款)
FEE = "fee"                  # 國外交易手續費/利息/違約金 → 進 expense
ANNUAL_FEE = "annual_fee"    # 年費 → 進 expense
FEE_WAIVER = "fee_waiver"    # 費用減免 (年費/手續費/利息減免; 銀行本來要收後來不收) → 進 income
INSTALLMENT = "installment"  # 分期付款 → 進 expense (本期攤)
UNKNOWN = "unknown"          # 無法歸類 → 統計層保守: amount>=0 進 income, amount<0 進 expense

# All recognised values (for tests / schema validation).
ALL_TXN_TYPES = frozenset({
    SPENDING, CASHBACK, REFUND, PAYMENT, FEE, ANNUAL_FEE, FEE_WAIVER, INSTALLMENT, UNKNOWN,
})

# --- keyword constants (中文 + 英文混合, 各家銀行 desc 都會碰到) ---
# 順序敏感: classifier 由特殊 → 一般依序測試, 命中即返回。

INSTALLMENT_KW = ("分期",)
ANNUAL_FEE_KW = (
    "年費",
    # 英文年費 desc: 聯邦 ****7027 「ANNUAL MEMBERSHIP FEE」/ Amex 「Annual Fee」
    # 也可能只寫 「MEMBER FEE」（會員費）
    # 注意順序: 這些較長 keyword 要在 FEE_KW 的「FEE」之前判 (installment→annual→fee),
    # 所以 classifier 順序把 ANNUAL_FEE 排在 FEE 前面。
    "ANNUAL FEE", "Annual Fee", "annual fee",
    "ANNUAL MEMBERSHIP", "Annual Membership", "annual membership",
    "ANNUAL MEMBER", "Annual Member",
    "MEMBER FEE", "Member Fee",
)
FEE_KW = (
    "國外交易手續費", "手續費", "利息", "違約金",
    "Fee", "FEE", "Interest", "INTEREST", "Penalty",
)
PAYMENT_KW = (
    # 中文
    "自動扣繳", "本行扣繳", "自扣", "自動扣款", "提款機繳款",
    "網路銀行繳款", "全國繳費網繳款", "繳款", "繳費", "扣繳",
    "銀行帳戶自動轉帳", "信用卡款",
    # 英文 (HSBC posted 可能 mix)
    "Payment", "PAYMENT", "Repayment",
)
CASHBACK_KW = (
    # 中文
    "回饋", "刷卡金", "滿額贈", "推薦活動", "現金回饋",
    "消費回饋",
    # 英文
    "CASHBACK", "Cashback", "CASH BACK", "CB_", "REWARD",
    "Reward", "ARIGATO",   # JCB_CB_ARIGATO_10% 也是回饋活動
)
REFUND_KW = (
    "退款", "退費", "退貨", "退",
    "REFUND", "Refund", "REVERSAL", "Reversal",
)

# 特殊 phrase — 優先於一般 keyword 判 (避免「年費減免」先被 ANNUAL_FEE_KW 的「年費」命中).
# 銀行對「費用被退回」的 desc 寫法各異, 但語意都是 FEE_WAIVER (income), 不是 annual_fee (expense).
# 加新 phrase 時, 挑「不會誤傷正常 annual_fee row」的字串: 「減免/沖銷/退回」都是明確
# 「銀行退還已收費用」的訊號, 加進來安全.
#
# 為什麼是 FEE_WAIVER 不是 REFUND (2026-07-04 使用者指示):
#   REFUND 語意是「商家退款」(user 買了東西退貨), FEE_WAIVER 語意是「銀行減免費用」
#   (銀行本來要收年費/手續費/利息, 決定不收). 兩件事在 stats 層應該可以分開撈,
#   例如「今年信用卡年費付了多少、減免了多少、淨值多少」需要 FEE_WAIVER 是乾淨的桶.
FEE_WAIVER_PHRASE = (
    "年費減免", "年費沖銷", "年費退回", "年費退款",
    "手續費減免", "手續費退回", "手續費沖銷",
    "利息減免", "利息退回", "利息沖銷",
    "年費 減免", "年費 沖銷",  # 有些 desc 中間夾空格
)


def classify_by_desc_and_sign(
    desc: str | None,
    amount_signed: float | int | None,
) -> str:
    """Fallback classifier: 純靠 desc 關鍵字 + 金額正負號判斷 txn_type.

    使用順序 (特殊 → 一般):
      0. 「年費減免/手續費減免/利息減免」whole-phrase → fee_waiver
         (優先於 ANNUAL_FEE_KW, FEE_KW; 避免 desc 含「年費」+「減免」被誤判 annual_fee)
      1. 「分期」→ installment (跟金額符號無關)
      2. 「年費」→ annual_fee
      3. 「手續費/利息/違約金」→ fee
      4. 「自動扣繳/繳款」→ payment
      5. 「回饋/刷卡金/cashback/CB_」→ cashback
      6. 「退款/退費/Refund」→ refund
      7. 純看符號:
         - amount > 0 → spending (一般刷卡)
         - amount < 0 → refund (沒關鍵字的負值, 保守當退款而非 cashback)
         - amount == 0 → unknown (sinopac 永豐有「大戶回饋 0 元」掛卡列, 由各家 short-circuit 處理)
         - amount is None → unknown
    """
    d = (desc or "")

    # Rule 0: 特殊 phrase 優先 (「年費減免」等 fee waiver phrase 必須擋在 ANNUAL_FEE_KW/FEE_KW 之前)
    if any(p in d for p in FEE_WAIVER_PHRASE):
        return FEE_WAIVER

    if any(k in d for k in INSTALLMENT_KW):
        return INSTALLMENT
    if any(k in d for k in ANNUAL_FEE_KW):
        return ANNUAL_FEE
    if any(k in d for k in FEE_KW):
        return FEE
    if any(k in d for k in PAYMENT_KW):
        return PAYMENT
    if any(k in d for k in CASHBACK_KW):
        return CASHBACK
    if any(k in d for k in REFUND_KW):
        return REFUND

    # 純符號 fallback
    if amount_signed is None:
        return UNKNOWN
    try:
        amt = float(amount_signed)
    except (TypeError, ValueError):
        return UNKNOWN
    if amt > 0:
        return SPENDING
    if amt < 0:
        # 沒關鍵字 + 負值: 保守標 refund (不假設成 cashback, 避免誤算)
        return REFUND
    return UNKNOWN  # amt == 0


def classify_ubot(tx_code: str | None, desc: str | None,
                  amount_signed: float | int | None) -> str:
    """ubot short-circuit: txCode 優先, fallback 給通用 classifier.

    探勘所知 txCode:
      20 → 還款/自扣 (payment)
      40 → 一般消費 TWD (spending)
      41 → 一般消費 (可能外幣, 或活動回饋如 JCB_CB_ARIGATO_10%) → 落回 fallback
      43 → 現金回饋 (cashback)
      55 → 貸記/退款/年費減免 (refund) — 2026-07-04 使用者確認:
           "微風無限卡正卡年費減免" txAmt=-5000 txCode=55, 配對同期 txCode=60
           "ANNUAL MEMBERSHIP FEE" +5000; 55 是聯邦標記「這筆是退款/沖銷」的訊號,
           必須認出來走 REFUND (income) 而不是掉回 fallback 讓「年費」desc 命中
           annual_fee (expense) 誤算成負支出。
      60 → 借記/一般費用 (fee 或 annual_fee) — 順序敏感: 讓 desc keyword 先判,
           若 desc 是「ANNUAL MEMBERSHIP FEE」歸 annual_fee, 否則保守歸 FEE。
    """
    code = (tx_code or "").strip()
    if code == "43":
        return CASHBACK
    if code == "20":
        return PAYMENT
    if code == "55":
        # 貸記/退款/沖銷 — 若 desc 是「年費/手續費/利息」相關的減免/沖銷/退回, 走 FEE_WAIVER
        # (銀行減免費用), 否則走 REFUND (商家退款). 走 desc-only classifier 讓
        # FEE_WAIVER_PHRASE 優先 rule 命中的即為 fee waiver, 其他情況才 fallback REFUND.
        # 若 desc 不含任何 keyword, 掉到 amount<0 → REFUND (原保守行為).
        result = classify_by_desc_and_sign(desc, amount_signed)
        if result == FEE_WAIVER:
            return FEE_WAIVER
        return REFUND
    if code == "60":
        # 讓 desc keyword 先判 (「ANNUAL MEMBERSHIP FEE」→ annual_fee, 「國外交易手續費」→ fee).
        # 命中不了才 fallback 純符號 (預設 spending).
        # 保守策略: 若 fallback 到 SPENDING (沒 keyword 命中), 覆寫回 FEE — code=60 本身
        # 就是「費用」訊號, 空 desc 也該歸 fee.
        result = classify_by_desc_and_sign(desc, amount_signed)
        if result == SPENDING:
            return FEE
        return result
    # txCode=40 或 41 都走 fallback (41 可能是 cashback 活動 desc, 必須讓 keyword 判)
    return classify_by_desc_and_sign(desc, amount_signed)


def classify_ctbc(tx_code: str | None, desc: str | None,
                  amount_signed: float | int | None) -> str:
    """ctbc short-circuit: txCode 主要訊號.

    探勘所知 txCode:
      20 → 還款 (payment) — purchaseAmt < 0, authCode='', cardNo='0000'
      40 → 一般消費 (spending)
      60 → 國外交易手續費 (fee)
    """
    code = (tx_code or "").strip()
    if code == "20":
        return PAYMENT
    if code == "60":
        return FEE
    if code == "40":
        # 40 是一般消費但仍 fallback (可能 desc 含「分期」「年費」要先濾)
        return classify_by_desc_and_sign(desc, amount_signed)
    # 未知 txCode → fallback
    return classify_by_desc_and_sign(desc, amount_signed)


def classify_hsbc(is_positive: bool | None, desc: str | None,
                  amount_signed: float | int | None) -> str:
    """hsbc short-circuit: isPositive bool + desc keyword.

    HSBC 命名反直覺:
      isPositive=True  → 借記 (消費/手續費/年費) → 主要 spending/fee/annual_fee
      isPositive=False → 貸記 (還款/退款/回饋)   → 主要 payment/refund/cashback

    desc keyword 優先 (因為 isPositive 只給「借記 vs 貸記」二分類, 不夠細),
    沒 keyword 命中才用 isPositive 當粗 fallback.
    """
    d = (desc or "")

    # desc keyword 優先 (跟 fallback 一樣的順序)
    if any(k in d for k in INSTALLMENT_KW):
        return INSTALLMENT
    if any(k in d for k in ANNUAL_FEE_KW):
        return ANNUAL_FEE
    if any(k in d for k in FEE_KW):
        return FEE
    if any(k in d for k in PAYMENT_KW):
        return PAYMENT
    if any(k in d for k in CASHBACK_KW):
        return CASHBACK
    if any(k in d for k in REFUND_KW):
        return REFUND

    # desc 沒 keyword: 用 isPositive 當粗分類
    if is_positive is True:
        return SPENDING
    if is_positive is False:
        # 貸記但無 keyword: 保守標 refund (商家退款 desc 通常是商家名沒 keyword)
        return REFUND

    # is_positive=None: 退回純符號 fallback
    return classify_by_desc_and_sign(desc, amount_signed)


