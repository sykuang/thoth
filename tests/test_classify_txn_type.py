"""驗證 backend/core/classify.py 的分類 helper.

Phase 6 (B-full): cashback/refund/payment 等 txn_type 分類, 給統計層用,
避免把銀行給的負值「回饋」當成「消費」灌水進 expense。

涵蓋:
  1. classify_by_desc_and_sign — fallback classifier (純 desc + 符號)
  2. classify_ubot              — txCode short-circuit (43=cashback, 20=payment, 60=fee)
  3. classify_ctbc              — txCode short-circuit (20=payment, 40=spending, 60=fee)
  4. classify_hsbc              — isPositive + desc keyword 2 段式
  5. 邊界 case (None / 0 / 全空 desc / 同時命中多 keyword)
"""
from __future__ import annotations


from backend.core import classify
from backend.core.classify import (
    ANNUAL_FEE,
    CASHBACK,
    FEE,
    FEE_WAIVER,
    INSTALLMENT,
    PAYMENT,
    REFUND,
    SPENDING,
    UNKNOWN,
)


# ====================================================================
# classify_by_desc_and_sign (fallback)
# ====================================================================

class TestFallbackKeyword:
    """desc keyword 命中順序: installment → annual_fee → fee → payment → cashback → refund → sign."""

    def test_installment_keyword(self) -> None:
        assert classify.classify_by_desc_and_sign("分期－雄獅旅行社", 58900) == INSTALLMENT

    def test_annual_fee_keyword(self) -> None:
        assert classify.classify_by_desc_and_sign("年費", 8000) == ANNUAL_FEE

    def test_annual_fee_waiver_is_fee_waiver_not_annual_fee(self) -> None:
        """Regression (0.3.65 backfill): 「年費減免」whole-phrase 必須擋在 ANNUAL_FEE_KW 前.

        Real evidence: 聯邦 ****7027 stmt 20260703 有 desc「微風無限卡正卡年費減免」,
        amount=-5000。migration backfill 走 desc-only classifier (無 txCode 訊號),
        修前只命中「年費」→ annual_fee → UI 顯示紅色 expense; 應為 fee_waiver (income, 綠色).
        新加 FEE_WAIVER_PHRASE ('年費減免/沖銷/退回/手續費減免/利息減免') 優先於
        ANNUAL_FEE_KW/FEE_KW 判. 為什麼是 fee_waiver 不是 refund: refund=商家退款,
        fee_waiver=銀行減免費用, 語意分開才能在 stats 撈「今年年費付了多少 vs 減免多少」.
        """
        assert classify.classify_by_desc_and_sign("微風無限卡正卡年費減免", -5000) == FEE_WAIVER
        assert classify.classify_by_desc_and_sign("年費減免", -5000) == FEE_WAIVER
        assert classify.classify_by_desc_and_sign("年費退回", -3000) == FEE_WAIVER
        assert classify.classify_by_desc_and_sign("年費沖銷", -1200) == FEE_WAIVER
        assert classify.classify_by_desc_and_sign("手續費減免", -200) == FEE_WAIVER
        assert classify.classify_by_desc_and_sign("利息減免", -50) == FEE_WAIVER

    def test_normal_annual_fee_still_annual_fee(self) -> None:
        """FEE_WAIVER_PHRASE 加入後不能誤傷正常年費 desc (無「減免/沖銷/退回」).

        「信用卡年費」「白金年費」等純年費描述, amount>0, 仍應歸 annual_fee (expense).
        """
        assert classify.classify_by_desc_and_sign("信用卡年費", 3000) == ANNUAL_FEE
        assert classify.classify_by_desc_and_sign("白金卡年費", 8000) == ANNUAL_FEE
        assert classify.classify_by_desc_and_sign("年費", 8000) == ANNUAL_FEE

    def test_fee_keyword_foreign_tx(self) -> None:
        assert classify.classify_by_desc_and_sign("國外交易手續費ＡＬＰ＊Ｔａｏｂａｏ", 192) == FEE

    def test_fee_keyword_interest(self) -> None:
        assert classify.classify_by_desc_and_sign("減少消費款利息", 241) == FEE

    def test_payment_keyword_auto_deduct(self) -> None:
        assert classify.classify_by_desc_and_sign("匯豐銀行自動扣款", -7137) == PAYMENT

    def test_payment_keyword_self_deduct(self) -> None:
        assert classify.classify_by_desc_and_sign("永豐自扣已入帳，謝謝！", -69) == PAYMENT

    def test_payment_keyword_atm(self) -> None:
        assert classify.classify_by_desc_and_sign("他行提款機繳款（轉帳）", -33645) == PAYMENT

    def test_cashback_keyword_chinese(self) -> None:
        assert classify.classify_by_desc_and_sign("刷卡現金回饋－日本指定商店", -15) == CASHBACK

    def test_cashback_keyword_credit(self) -> None:
        assert classify.classify_by_desc_and_sign("推薦活動刷卡金", -1000) == CASHBACK

    def test_cashback_keyword_arigato(self) -> None:
        """JCB_CB_ARIGATO_10% 是真實聯邦回饋活動 desc, 必須認得。"""
        assert classify.classify_by_desc_and_sign("JCB_CB_ARIGATO_10% TOKYO JP", -1965) == CASHBACK

    def test_cashback_keyword_cb_prefix(self) -> None:
        """`CB_` 前綴 (cashback 縮寫) 必須認得。"""
        assert classify.classify_by_desc_and_sign("某 CB_REWARD_50%", -50) == CASHBACK

    def test_refund_keyword_chinese(self) -> None:
        assert classify.classify_by_desc_and_sign("退款－蝦皮訂單", -300) == REFUND

    def test_refund_keyword_english(self) -> None:
        assert classify.classify_by_desc_and_sign("MERCHANT REFUND", -500) == REFUND


class TestFallbackSignOnly:
    """無 keyword 命中時純靠符號判定."""

    def test_positive_amount_no_keyword(self) -> None:
        assert classify.classify_by_desc_and_sign("某商家", 1234) == SPENDING

    def test_negative_amount_no_keyword(self) -> None:
        """沒關鍵字 + 負值 → refund (保守, 不假設 cashback)."""
        assert classify.classify_by_desc_and_sign("不明", -500) == REFUND

    def test_zero_amount_no_keyword(self) -> None:
        """amount=0 是 sinopac 大戶回饋特例, fallback 給 unknown 讓 short-circuit 處理."""
        assert classify.classify_by_desc_and_sign("大戶 PL 消費回饋入帳戶", 0) == CASHBACK

    def test_zero_amount_no_keyword_no_cashback(self) -> None:
        assert classify.classify_by_desc_and_sign("不明", 0) == UNKNOWN

    def test_none_amount(self) -> None:
        assert classify.classify_by_desc_and_sign("某商家", None) == UNKNOWN

    def test_none_desc_positive(self) -> None:
        assert classify.classify_by_desc_and_sign(None, 100) == SPENDING

    def test_empty_desc_negative(self) -> None:
        assert classify.classify_by_desc_and_sign("", -100) == REFUND


class TestFallbackPriorityOrder:
    """順序敏感: 多 keyword 同時命中時, 優先順序固定."""

    def test_installment_beats_cashback(self) -> None:
        """「分期滿額贈刷卡金」同時有「分期」跟「刷卡金」, 應該是 installment."""
        # 雖然 HSBC 把它當 cashback (因為是贈金), 但純 fallback 看順序就是 installment.
        # 真實生產用 classify_hsbc, 那裡 desc 順序也是先 installment, 與這裡一致.
        result = classify.classify_by_desc_and_sign("分期滿額贈刷卡金", 700)
        assert result == INSTALLMENT

    def test_annual_fee_beats_payment(self) -> None:
        """「年費自動扣繳」同時有「年費」「扣繳」, 應該是 annual_fee."""
        assert classify.classify_by_desc_and_sign("年費自動扣繳", 8000) == ANNUAL_FEE

    def test_fee_beats_cashback(self) -> None:
        """「手續費回饋」實務上不該存在, 但 fee 必須贏 (因為 fee 順序在前)."""
        assert classify.classify_by_desc_and_sign("手續費回饋", 100) == FEE


# ====================================================================
# classify_ubot (txCode short-circuit + fallback)
# ====================================================================

class TestUbotShortCircuit:
    def test_txcode_43_is_cashback(self) -> None:
        """聯邦 txCode=43 = 現金回饋 (純 txCode 判定, desc 不看)."""
        assert classify.classify_ubot("43", "刷卡現金回饋－日本指定商店", -15) == CASHBACK

    def test_txcode_43_even_without_keyword(self) -> None:
        """txCode=43 在 desc 沒明顯 keyword 時也直接判 cashback."""
        assert classify.classify_ubot("43", "某不明 desc", -500) == CASHBACK

    def test_txcode_20_is_payment(self) -> None:
        assert classify.classify_ubot("20", "本行扣繳", -8600) == PAYMENT

    def test_txcode_60_is_fee(self) -> None:
        assert classify.classify_ubot("60", "國外交易手續費", 192) == FEE

    def test_txcode_40_normal_spending(self) -> None:
        """txCode=40 一般消費 → fallback → 正值 → spending."""
        assert classify.classify_ubot("40", "微風信義", 29) == SPENDING

    def test_txcode_41_arigato_cashback(self) -> None:
        """關鍵案例: 聯邦 ****7027 JCB_CB_ARIGATO_10% txCode=41 但 desc 含 CB_, 必須是 cashback."""
        assert classify.classify_ubot(
            "41", "JCB_CB_ARIGATO_10% TOKYO JP", -1965
        ) == CASHBACK

    def test_txcode_41_normal_foreign_spending(self) -> None:
        """txCode=41 沒 cashback keyword + 正值 → spending."""
        assert classify.classify_ubot(
            "41", "AMAZON.CO.JP TOKYO JP", 5000
        ) == SPENDING

    def test_txcode_55_annual_fee_refund_must_be_fee_waiver(self) -> None:
        """關鍵案例 (使用者 2026-07-04 反映 & real raw 確認):

        聯邦 ****7027 stmt 20260703 有一筆:
          txCode=55  txAmt=-5000  txDesc='微風無限卡正卡年費減免'
        配對同期扣款:
          txCode=60  txAmt=+5000  txDesc='ANNUAL MEMBERSHIP FEE'
        兩筆淨額 0 = 年費全額減免。

        修前 bug: txCode=55 沒 short-circuit → 掉回 fallback →「年費」desc 命中
        ANNUAL_FEE (expense) → UI 顯示 -NT$5,000 紅色, 使用者以為是「兩筆都扣年費」。
        修後 v2 (2026-07-04 Layer 1): txCode=55 + 「年費減免」desc → FEE_WAIVER (非 REFUND).
        REFUND=商家退款, FEE_WAIVER=銀行減免費用, 語意獨立但 cashflow 都是 income (綠色).
        """
        assert classify.classify_ubot(
            "55", "微風無限卡正卡年費減免", -5000
        ) == FEE_WAIVER

    def test_txcode_55_with_generic_desc_still_refund(self) -> None:
        """55 若 desc 無 fee_waiver phrase 命中, fallback 走 REFUND (保守).

        (2026-07-04 Layer 1 refactor): 55 的默認語意仍是 refund, 只有明確
        「費用減免」phrase 才升級成 fee_waiver.
        """
        assert classify.classify_ubot("55", "年費退回", -3000) == FEE_WAIVER  # 有 fee_waiver phrase
        assert classify.classify_ubot("55", "任意 desc", -100) == REFUND  # 無 fee_waiver phrase
        assert classify.classify_ubot("55", "商家退款", -500) == REFUND  # refund keyword

    def test_txcode_60_annual_membership_fee_is_annual_fee(self) -> None:
        """配對案例: txCode=60 + desc='ANNUAL MEMBERSHIP FEE' → annual_fee (非泛 fee).

        修前 bug: code=60 硬歸 FEE, 讓「ANNUAL MEMBERSHIP FEE」失去年費分類意義.
        修後: code=60 讓 desc keyword 先判 → ANNUAL_FEE keyword 命中 → annual_fee.
        """
        assert classify.classify_ubot(
            "60", "ANNUAL MEMBERSHIP FEE", 5000
        ) == ANNUAL_FEE

    def test_txcode_60_foreign_tx_fee_still_fee(self) -> None:
        """code=60 + desc='國外交易手續費' → 走 desc keyword → FEE (跟前版一致)."""
        assert classify.classify_ubot(
            "60", "國外交易手續費", 192
        ) == FEE

    def test_txcode_60_empty_desc_still_fee(self) -> None:
        """code=60 + desc 沒 keyword + 正值 → fallback 給 SPENDING, code=60 覆寫回 FEE.

        code=60 本身就是「費用」訊號, 就算 desc 空/無 keyword 也不該當一般消費.
        """
        assert classify.classify_ubot("60", "某未知費用", 100) == FEE
        assert classify.classify_ubot("60", "", 50) == FEE

    def test_empty_txcode_uses_fallback(self) -> None:
        assert classify.classify_ubot("", "刷卡現金回饋", -100) == CASHBACK
        assert classify.classify_ubot(None, "微風信義", 29) == SPENDING


# ====================================================================
# classify_ctbc (txCode short-circuit + fallback)
# ====================================================================

class TestCtbcShortCircuit:
    def test_txcode_20_is_payment(self) -> None:
        assert classify.classify_ctbc("20", "本行扣繳", -8600) == PAYMENT

    def test_txcode_60_is_fee(self) -> None:
        assert classify.classify_ctbc("60", "國外交易手續費", 109) == FEE

    def test_txcode_40_spending(self) -> None:
        assert classify.classify_ctbc("40", "牌照稅單筆", 3744) == SPENDING

    def test_txcode_40_with_installment_keyword(self) -> None:
        """即使 txCode=40, 若 desc 含「分期」必須走 fallback 判 installment."""
        assert classify.classify_ctbc("40", "分期－雄獅旅行社", 58900) == INSTALLMENT


# ====================================================================
# classify_hsbc (isPositive + desc keyword)
# ====================================================================

class TestHsbcIsPositive:
    def test_positive_normal_spending(self) -> None:
        """isPositive=True + 無 keyword → spending."""
        assert classify.classify_hsbc(True, "ＡＰＥ１０１美食街", 21) == SPENDING

    def test_positive_with_fee_keyword(self) -> None:
        """isPositive=True + 「國外交易手續費」 → fee (keyword 優先)."""
        assert classify.classify_hsbc(True, "國外交易手續費ＡＬＰ＊Ｔａｏｂａｏ", 192) == FEE

    def test_positive_with_annual_fee(self) -> None:
        assert classify.classify_hsbc(True, "年費", 8000) == ANNUAL_FEE

    def test_negative_with_payment_keyword(self) -> None:
        assert classify.classify_hsbc(False, "匯豐銀行自動扣款", -7137) == PAYMENT

    def test_negative_with_cashback_keyword(self) -> None:
        assert classify.classify_hsbc(False, "推薦活動刷卡金", -1000) == CASHBACK

    def test_negative_with_installment_cashback(self) -> None:
        """HSBC 真實 case: 「２０２６Ｑ１分期滿額贈刷卡金」, isPositive=False.
        keyword 順序 installment > cashback, 所以歸 installment."""
        assert classify.classify_hsbc(
            False, "２０２６Ｑ１分期滿額贈刷卡金", -700
        ) == INSTALLMENT

    def test_negative_no_keyword_is_refund(self) -> None:
        """貸記 (isPositive=False) 但 desc 是商家名沒 keyword → 保守標 refund."""
        assert classify.classify_hsbc(
            False, "ＭＶＣＩＡＳＩＡＰＡＣＩＦＩＣ", -223180
        ) == REFUND

    def test_is_positive_none_falls_back(self) -> None:
        """is_positive=None → 退回純 desc+sign fallback."""
        assert classify.classify_hsbc(None, "刷卡現金回饋", -100) == CASHBACK
        assert classify.classify_hsbc(None, "某商家", 100) == SPENDING


# ====================================================================
# 真實事故場景 (regression for 聯邦 ****7027 JCB_CB_ARIGATO_10%)
# ====================================================================

class TestRealLiveData2026_06_14:
    """以使用者 2026-06-14 反映的 4 筆聯邦 ****7027 真實 billed data 為 regression."""

    def test_jcb_arigato_must_be_cashback_not_expense(self) -> None:
        """使用者指控: 「這個應該是正值 世信用卡回饋 為什麼顯示成負的」
        ⇒ txCode=41 + desc 含 CB_ + 負值 + 外幣 → 必須是 cashback, 否則統計層會把 1965 灌進 expense."""
        result = classify.classify_ubot(
            "41", "JCB_CB_ARIGATO_10%       TOKYO        JP", -1965
        )
        assert result == CASHBACK

    def test_arigato_other_two_must_be_cashback(self) -> None:
        """另外 2 筆 txCode=43 的純現金回饋 desc 也必須是 cashback."""
        assert classify.classify_ubot(
            "43", "刷卡現金回饋－日本指定商店０４", -15
        ) == CASHBACK
        assert classify.classify_ubot(
            "43", "刷卡現金回饋－吉鶴卡日幣回饋０４", -403
        ) == CASHBACK

    def test_normal_spending_not_misclassified(self) -> None:
        """同卡其他 3 筆正常消費不能被誤判成 cashback/refund."""
        assert classify.classify_ubot("40", "微風信義", 29) == SPENDING
        assert classify.classify_ubot("40", "１１３年綜所稅款", 41030) == SPENDING


# ====================================================================
# 枚舉完整性 (確保我加新 txn_type 時 ALL_TXN_TYPES 同步)
# ====================================================================

class TestEnumCompleteness:
    def test_all_txn_types_contains_each_constant(self) -> None:
        for v in (SPENDING, CASHBACK, REFUND, PAYMENT, FEE, ANNUAL_FEE, FEE_WAIVER, INSTALLMENT, UNKNOWN):
            assert v in classify.ALL_TXN_TYPES

    def test_no_classifier_returns_unrecognized_value(self) -> None:
        """所有 classifier 在各種輸入下回傳值必須在 ALL_TXN_TYPES 內."""
        cases = [
            classify.classify_by_desc_and_sign("test", 100),
            classify.classify_by_desc_and_sign("test", -100),
            classify.classify_by_desc_and_sign("test", 0),
            classify.classify_by_desc_and_sign("test", None),
            classify.classify_by_desc_and_sign(None, 100),
            classify.classify_ubot("43", "x", -100),
            classify.classify_ubot("40", "x", 100),
            classify.classify_ubot(None, None, None),
            classify.classify_ctbc("20", "x", -100),
            classify.classify_ctbc(None, None, None),
            classify.classify_hsbc(True, "x", 100),
            classify.classify_hsbc(False, "x", -100),
            classify.classify_hsbc(None, "x", 0),
        ]
        for c in cases:
            assert c in classify.ALL_TXN_TYPES, f"unrecognized: {c}"
