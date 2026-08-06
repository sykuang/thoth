"""帳戶類型統一分類（Account Product Type Classifier）。

11 家銀行 raw JSON 結構差異極大，但分類成下面這幾類後，下游
(balance_history / portfolio router / dashboard) 就能用單一規則處理。

設計原則：
  - 詳細版 (使用者 2026-06-14 拍板)：deposit / time_deposit / fx_deposit /
    checking / loan / mortgage / credit_line / investment / unknown
  - 「資產類」(deposit/time_deposit/fx_deposit/checking)：可灌進 total_assets
  - 「負債類」(loan/mortgage/credit_line)：要灌進 total_liabilities
  - 「投資類」(investment)：另外計算，不混進現金資產
  - unknown 保守處理：不灌任何總額，dashboard 顯示 N/A

每家銀行的 raw account dict 結構不同，這裡用 dispatch dict 對應各家。
classify_account(bank, raw) → ProductType.X
"""
from __future__ import annotations



# ============================================================
# Product Type 常量（使用者 2026-06-14 拍板「詳細版」）
# ============================================================

class ProductType:
    """帳戶業務類型常量。

    持久層 accounts.product_type 欄位的合法值。
    """
    DEPOSIT = "deposit"               # 活儲 / 活期 / 一般存款 (TWD)
    TIME_DEPOSIT = "time_deposit"     # 定存
    FX_DEPOSIT = "fx_deposit"         # 外幣存款（外幣活期/定期都算）
    CHECKING = "checking"             # 支票存款 / 支存
    LOAN = "loan"                     # 個人信貸 / 通用貸款（無明確擔保品）
    MORTGAGE = "mortgage"             # 房貸 (有不動產擔保)
    CREDIT_LINE = "credit_line"       # 信用額度 (透支等, 用了才產生負債)
    INVESTMENT = "investment"         # 基金 / 股票 / 信託
    UNKNOWN = "unknown"               # 不確定（保守處理，不計入總額）


ASSET_TYPES = frozenset({
    ProductType.DEPOSIT,
    ProductType.TIME_DEPOSIT,
    ProductType.FX_DEPOSIT,
    ProductType.CHECKING,
})
"""可計入 total_assets 的類型。"""

LIABILITY_TYPES = frozenset({
    ProductType.LOAN,
    ProductType.MORTGAGE,
    ProductType.CREDIT_LINE,
})
"""可計入 total_liabilities 的類型（注意：信用卡負債走另一條 card pipeline）。"""


# ============================================================
# Helpers
# ============================================================

def is_asset_type(product_type: str | None) -> bool:
    """是不是「正資產」（會灌進 total_assets）。"""
    return product_type in ASSET_TYPES


def is_liability_type(product_type: str | None) -> bool:
    """是不是「負債」（會灌進 total_liabilities）。"""
    return product_type in LIABILITY_TYPES


def normalize_liability_magnitude(
    balance: int | float | None,
) -> int | float | None:
    """統一 aggregate liability：永遠用正數規模供 assets - liabilities。"""
    return abs(balance) if balance is not None else None


def normalize_account_balance(
    product_type: str | None,
    balance: int | float | None,
) -> int | float | None:
    """統一帳戶餘額符號：負債帳戶為負，其他類型保留銀行原值。

    只處理 account-level balance；`loan_balance` / `total_liabilities` 是供
    aggregate 扣除的正數規模，不走這個 helper。
    """
    if balance is None:
        return None
    return -abs(balance) if is_liability_type(product_type) else balance


# ============================================================
# Per-bank classifiers
# ============================================================
#
# 每個 classifier 拿 raw account dict（各家 schema 不同），回傳 ProductType.X。
# 偵察結果見 phase 1 report — 三類銀行：
#   - 群組 A：原始資料已自帶類型（cathay/dbs/sinopac/ubot），直接 mapping
#   - 群組 B：需 keyword 偵測（scsb/linebank/ctbc/esun）
#   - 群組 C：沒抓到帳戶資料（hsbc/fubon/scb/taishin），不會走 classifier

# 全銀行通用的 keyword 偵測（後備方案）
_LOAN_KEYWORDS = (
    "貸款", "信貸", "個人信貸", "信用貸款", "Loan", "loan", "LOAN",
)
_MORTGAGE_KEYWORDS = (
    "房貸", "住宅貸款", "不動產貸款", "Mortgage", "mortgage", "MORTGAGE",
)
_TIME_DEPOSIT_KEYWORDS = (
    "定存", "定期存款", "定期儲蓄", "Time Deposit", "Fixed Deposit",
)
_CHECKING_KEYWORDS = (
    "支存", "支票存款", "Checking",
)
_INVESTMENT_KEYWORDS = (
    "基金", "信託", "證券", "Investment", "Trust", "Fund",
)


def classify_by_keyword(text: str | None, currency: str | None = None) -> str:
    """純 keyword + currency 推類型。後備方案，每家 classifier 走完還沒分出來就用。

    Order matters：mortgage 在 loan 之前查（房貸 ⊂ 貸款），
    time_deposit 在 deposit 之前查（定期存款 ⊂ 存款）。
    """
    if not text:
        text = ""
    t = text

    # 1. 房貸先查（"房貸" 字串若先被 "貸款" 規則吞掉就分不出來）
    for kw in _MORTGAGE_KEYWORDS:
        if kw in t:
            return ProductType.MORTGAGE
    # 2. 信貸 / 一般 loan
    for kw in _LOAN_KEYWORDS:
        if kw in t:
            return ProductType.LOAN
    # 3. 投資
    for kw in _INVESTMENT_KEYWORDS:
        if kw in t:
            return ProductType.INVESTMENT
    # 4. 支存
    for kw in _CHECKING_KEYWORDS:
        if kw in t:
            return ProductType.CHECKING
    # 5. 定存（要在「活期」前判斷，因「定期存款」也含「存款」）
    for kw in _TIME_DEPOSIT_KEYWORDS:
        if kw in t:
            return ProductType.TIME_DEPOSIT
    # 6. 外幣判斷（拿 currency 輔助 + 文字含「外幣/Foreign」）
    if currency and currency not in ("TWD", "新台幣", "台幣"):
        return ProductType.FX_DEPOSIT
    if "外幣" in t or "Foreign" in t or "FX" in t:
        return ProductType.FX_DEPOSIT
    # 7. 含「活儲/活期/存款/Deposit」→ deposit
    if any(k in t for k in ("活儲", "活期", "存款", "Deposit", "deposit")):
        return ProductType.DEPOSIT
    # 8. 真不知道
    return ProductType.UNKNOWN


# --------- 群組 A：原始資料已自帶 ---------

def classify_ubot(raw: dict) -> str:
    """ubot：deposit_twd.NTList / FTList / LoanList 三組獨立 array。

    persist.py 呼叫時應該已經知道 raw 來自哪個 list (caller 傳 hint)，
    這 classifier 用 _list_origin field 區分（caller 自己塞）。
    """
    origin = raw.get("_list_origin")  # NTList / FTList / LoanList
    if origin == "LoanList":
        # ubot loan 沒 explicit 房貸/信貸區分，預設 loan
        # 若 raw 有 AccountType 含「房貸」可進一步分
        acct_type = raw.get("AccountType") or ""
        if "房貸" in acct_type or "mortgage" in acct_type.lower():
            return ProductType.MORTGAGE
        return ProductType.LOAN
    if origin == "FTList":
        return ProductType.FX_DEPOSIT
    if origin == "NTList":
        # TWD 活期 / 定存
        acct_type = raw.get("AccountType") or ""
        return classify_by_keyword(acct_type, currency="TWD")
    # fallback
    return classify_by_keyword(raw.get("AccountType"), currency=raw.get("currency"))


def classify_cathay(raw: dict) -> str:
    """cathay：data.accounts[] 主表 + data.loan.accounts[] 獨立貸款表。

    persist.py 呼叫時用 _source hint 區分（"accounts" 或 "loan_accounts"）。
    """
    source = raw.get("_source")
    if source == "loan_accounts":
        # 國泰貸款帳號預設 loan，若名稱含房貸關鍵字才升級 mortgage
        name = (raw.get("name") or raw.get("loanName")
                or raw.get("product") or raw.get("type") or "")
        if any(kw in name for kw in _MORTGAGE_KEYWORDS):
            return ProductType.MORTGAGE
        return ProductType.LOAN
    # 主表：用 type / product_type / currency 推
    acct_type = raw.get("type") or raw.get("AccountType") or ""
    return classify_by_keyword(acct_type, currency=raw.get("currency"))


def classify_dbs(raw: dict) -> str:
    """dbs：schemeName / schemeType / schemeCode + _source hint。"""
    source = raw.get("_source")
    if source == "loan":
        return ProductType.LOAN  # dbs 暫無細分
    scheme_name = raw.get("schemeName") or ""
    scheme_type = raw.get("schemeType") or ""
    scheme_code = (raw.get("schemeCode") or "").upper()
    # schemeCode FDASA / FDODA = 外幣存款；ODA = 一般存款
    if "FD" in scheme_code or "FX" in scheme_code:
        return ProductType.FX_DEPOSIT
    # 看 schemeName 中文判斷
    by_name = classify_by_keyword(scheme_name, currency=raw.get("currency"))
    if by_name != ProductType.UNKNOWN:
        return by_name
    return classify_by_keyword(scheme_type, currency=raw.get("currency"))


def classify_sinopac(raw: dict) -> str:
    """sinopac：AcctText 文字明確 + debit_accounts 獨立 loan 路徑。

    AcctText 範例：「營業部DAWHO活期儲蓄存款」/「營業部DAWHO外幣組合存款」
    """
    source = raw.get("_source")
    if source == "debit_accounts":
        return ProductType.LOAN
    acct_text = raw.get("AcctText") or ""
    return classify_by_keyword(acct_text, currency=raw.get("currency"))


# --------- 群組 B：需 keyword 偵測 ---------

def classify_scsb(raw: dict) -> str:
    """scsb：overview_text 切塊時就應該已經把 type_header 塞進 raw."""
    type_header = raw.get("type_header") or raw.get("section_label") or ""
    return classify_by_keyword(type_header, currency=raw.get("currency"))


def classify_linebank(raw: dict) -> str:
    """linebank：account_options 本身無 type，靠 desc keyword fallback.

    （2026-06-15 拔除 _source=loan_inferred short-circuit — 對應 persist.py
    拔合成 linebank_loan_inferred row 一起做；LINE Bank raw 沒有 loan endpoint，
    僅靠 transactions 內「分期信貸」rmk 字串合成假帳戶是錯的。未來真有 loan
    endpoint API 開放時再加回 _source hint。）
    """
    desc = raw.get("desc") or raw.get("text") or ""
    return classify_by_keyword(desc, currency=raw.get("currency"))


def classify_ctbc(raw: dict) -> str:
    """ctbc：acctType 數字碼分類。

    acctType '00' = 活儲、03 = 支存、04 = 定存、05 = 外幣存款；
    未列入碼走 keyword fallback。

    （2026-06-14 拔除 loan_summary short-circuit — 對應 persist.py 拔合成
    ctbc_loan_summary row 一起做；未來真要回來爬信貸已動用時再加回 _source hint。）
    """
    # acctType 數字碼 short-circuit（中信常見 code）
    acct_type_code = (raw.get("acctType") or "").strip()
    code_map = {
        "00": ProductType.DEPOSIT,
        "03": ProductType.CHECKING,
        "04": ProductType.TIME_DEPOSIT,
        "05": ProductType.FX_DEPOSIT,
    }
    if acct_type_code in code_map:
        return code_map[acct_type_code]
    # 其他狀況試 keyword（type 帶中文時可命中）
    return classify_by_keyword(raw.get("type") or raw.get("acctType"),
                                currency=raw.get("currency"))


def classify_esun(raw: dict) -> str:
    """esun：category 文字明確（"臺幣綜存" / "外幣活存"）。"""
    category = raw.get("category") or ""
    return classify_by_keyword(category, currency=raw.get("currency"))


# --------- 群組 C：no-op（純信用卡）---------

def classify_hsbc(raw: dict) -> str:
    """hsbc：只有信用卡，理論上不會走帳戶 classifier。"""
    return classify_by_keyword(raw.get("type"), currency=raw.get("currency"))


def classify_fubon(raw: dict) -> str:
    return classify_by_keyword(raw.get("type"), currency=raw.get("currency"))


def classify_scb(raw: dict) -> str:
    return classify_by_keyword(raw.get("type"), currency=raw.get("currency"))


def classify_taishin(raw: dict) -> str:
    """taishin SavingAccount[] = 存款帳戶（本來就是），keyword fallback 失敗時
    強制 default deposit 而不是 unknown（_source 進到 persist 那層）。"""
    pt = classify_by_keyword(raw.get("type") or raw.get("accountTypeName"),
                              currency=raw.get("currency"))
    if pt == ProductType.UNKNOWN:
        # SavingAccount = 存款（API 已分類）
        pt = (ProductType.FX_DEPOSIT if (raw.get("currency") or "TWD").upper() != "TWD"
              else ProductType.DEPOSIT)
    return pt


# ============================================================
# Dispatch
# ============================================================

_CLASSIFIERS = {
    "ubot": classify_ubot,
    "cathay": classify_cathay,
    "dbs": classify_dbs,
    "sinopac": classify_sinopac,
    "scsb": classify_scsb,
    "linebank": classify_linebank,
    "ctbc": classify_ctbc,
    "esun": classify_esun,
    "hsbc": classify_hsbc,
    "fubon": classify_fubon,
    "scb": classify_scb,
    "taishin": classify_taishin,
}


def classify_account(bank: str, raw: dict) -> str:
    """主入口：根據 bank 名稱 dispatch 到對應 classifier。

    使用者呼叫範例:
        from backend.core.account_classify import classify_account, ProductType

        product_type = classify_account("scsb", {
            "account_no": "90000000257044",
            "currency": "TWD",
            "type_header": "貸款",   # ← scsb _extract_accounts 切塊時塞的
        })
        # → ProductType.LOAN
    """
    fn = _CLASSIFIERS.get(bank)
    if not fn:
        # 未知銀行 → 純 keyword
        return classify_by_keyword(
            raw.get("type") or raw.get("AccountType") or raw.get("category"),
            currency=raw.get("currency"),
        )
    return fn(raw)
