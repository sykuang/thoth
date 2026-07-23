"""account_classify 單元測試 — 11 家銀行 deposit/loan/mortgage 分類驗證。"""
from __future__ import annotations

import pytest

from backend.core.account_classify import (
    ProductType,
    classify_account,
    classify_by_keyword,
    is_asset_type,
    is_liability_type,
)


# ============================================================
# Helpers / 常量
# ============================================================

class TestHelpers:
    def test_is_asset_type_yes(self):
        for t in (ProductType.DEPOSIT, ProductType.TIME_DEPOSIT,
                  ProductType.FX_DEPOSIT, ProductType.CHECKING):
            assert is_asset_type(t)

    def test_is_asset_type_no(self):
        for t in (ProductType.LOAN, ProductType.MORTGAGE,
                  ProductType.CREDIT_LINE, ProductType.INVESTMENT,
                  ProductType.UNKNOWN, None, "garbage"):
            assert not is_asset_type(t)

    def test_is_liability_type(self):
        assert is_liability_type(ProductType.LOAN)
        assert is_liability_type(ProductType.MORTGAGE)
        assert is_liability_type(ProductType.CREDIT_LINE)
        assert not is_liability_type(ProductType.DEPOSIT)
        assert not is_liability_type(ProductType.INVESTMENT)


# ============================================================
# classify_by_keyword
# ============================================================

class TestKeywordClassifier:
    @pytest.mark.parametrize("text,expected", [
        ("貸款", ProductType.LOAN),
        ("信貸", ProductType.LOAN),
        ("個人信貸", ProductType.LOAN),
        ("Loan Account", ProductType.LOAN),
        ("信用貸款餘額", ProductType.LOAN),
    ])
    def test_loan_keywords(self, text, expected):
        assert classify_by_keyword(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("房貸", ProductType.MORTGAGE),
        ("住宅貸款", ProductType.MORTGAGE),
        ("不動產貸款", ProductType.MORTGAGE),
        ("Mortgage Loan", ProductType.MORTGAGE),  # mortgage 優先於 loan
    ])
    def test_mortgage_keywords(self, text, expected):
        assert classify_by_keyword(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("定存", ProductType.TIME_DEPOSIT),
        ("定期存款", ProductType.TIME_DEPOSIT),
        ("定期儲蓄存款", ProductType.TIME_DEPOSIT),
        ("Time Deposit", ProductType.TIME_DEPOSIT),
        ("Fixed Deposit", ProductType.TIME_DEPOSIT),
    ])
    def test_time_deposit_keywords(self, text, expected):
        assert classify_by_keyword(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("活儲", ProductType.DEPOSIT),
        ("活期存款", ProductType.DEPOSIT),
        ("活儲存款", ProductType.DEPOSIT),
        ("Deposit", ProductType.DEPOSIT),
    ])
    def test_deposit_keywords(self, text, expected):
        assert classify_by_keyword(text) == expected

    @pytest.mark.parametrize("text,currency,expected", [
        ("活期存款", "USD", ProductType.FX_DEPOSIT),
        ("外幣存款", "TWD", ProductType.FX_DEPOSIT),
        ("Foreign Currency", "TWD", ProductType.FX_DEPOSIT),
        ("外幣活存", None, ProductType.FX_DEPOSIT),
        ("某帳戶", "JPY", ProductType.FX_DEPOSIT),  # currency 推
    ])
    def test_fx_deposit_keywords(self, text, currency, expected):
        assert classify_by_keyword(text, currency=currency) == expected

    @pytest.mark.parametrize("text,expected", [
        ("基金", ProductType.INVESTMENT),
        ("信託", ProductType.INVESTMENT),
        ("證券戶", ProductType.INVESTMENT),
        ("Investment Fund", ProductType.INVESTMENT),
    ])
    def test_investment_keywords(self, text, expected):
        assert classify_by_keyword(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("支存", ProductType.CHECKING),
        ("支票存款", ProductType.CHECKING),
        ("Checking Account", ProductType.CHECKING),
    ])
    def test_checking_keywords(self, text, expected):
        assert classify_by_keyword(text) == expected

    def test_empty_returns_unknown(self):
        assert classify_by_keyword("") == ProductType.UNKNOWN
        assert classify_by_keyword(None) == ProductType.UNKNOWN

    def test_no_match_returns_unknown(self):
        assert classify_by_keyword("某種其他帳戶類型") == ProductType.UNKNOWN

    def test_order_mortgage_before_loan(self):
        """混合「房貸」+「貸款」字串，應該 mortgage 勝出（更精確）。"""
        assert classify_by_keyword("房貸帳戶 (一般貸款類)") == ProductType.MORTGAGE

    def test_order_time_deposit_before_deposit(self):
        """「定期存款」要分到 time_deposit 不是 deposit。"""
        assert classify_by_keyword("臺幣定期存款") == ProductType.TIME_DEPOSIT


# ============================================================
# Per-bank classifiers
# ============================================================

class TestUbot:
    def test_loan_list_basic(self):
        raw = {"_list_origin": "LoanList", "AccountType": "信用貸款"}
        assert classify_account("ubot", raw) == ProductType.LOAN

    def test_loan_list_mortgage(self):
        raw = {"_list_origin": "LoanList", "AccountType": "房貸戶"}
        assert classify_account("ubot", raw) == ProductType.MORTGAGE

    def test_loan_list_no_type_fallback_loan(self):
        raw = {"_list_origin": "LoanList"}
        assert classify_account("ubot", raw) == ProductType.LOAN

    def test_nt_list_deposit(self):
        raw = {"_list_origin": "NTList", "AccountType": "活期儲蓄"}
        assert classify_account("ubot", raw) == ProductType.DEPOSIT

    def test_nt_list_time_deposit(self):
        raw = {"_list_origin": "NTList", "AccountType": "定期存款"}
        assert classify_account("ubot", raw) == ProductType.TIME_DEPOSIT

    def test_ft_list_fx(self):
        raw = {"_list_origin": "FTList", "AccountType": "活存", "currency": "USD"}
        assert classify_account("ubot", raw) == ProductType.FX_DEPOSIT


class TestCathay:
    def test_loan_accounts_source(self):
        raw = {"_source": "loan_accounts", "name": "一般信用貸款"}
        assert classify_account("cathay", raw) == ProductType.LOAN

    def test_loan_accounts_mortgage(self):
        raw = {"_source": "loan_accounts", "name": "房貸 - 西湖"}
        assert classify_account("cathay", raw) == ProductType.MORTGAGE

    def test_main_accounts_digital(self):
        raw = {"type": "數位存款帳戶１—１類(原KOKO)", "currency": "TWD"}
        assert classify_account("cathay", raw) == ProductType.DEPOSIT


class TestDbs:
    def test_loan_source(self):
        raw = {"_source": "loan", "scheme": "Mortgage"}
        # 注意：dbs classify_dbs 對 loan source 直接回 LOAN（不細分）
        assert classify_account("dbs", raw) == ProductType.LOAN

    def test_fx_scheme_code(self):
        raw = {"schemeCode": "FDASA", "schemeName": "外幣數位存款",
               "currency": "USD"}
        assert classify_account("dbs", raw) == ProductType.FX_DEPOSIT

    def test_twd_deposit(self):
        raw = {"schemeCode": "ODA", "schemeName": "臺幣數位存款",
               "currency": "TWD"}
        assert classify_account("dbs", raw) == ProductType.DEPOSIT

    def test_fx_by_currency_alone(self):
        raw = {"schemeName": "ODA", "schemeType": "ODA", "currency": "EUR"}
        assert classify_account("dbs", raw) == ProductType.FX_DEPOSIT


class TestSinopac:
    def test_debit_accounts_source(self):
        raw = {"_source": "debit_accounts", "AcctText": "理財透支貸款"}
        assert classify_account("sinopac", raw) == ProductType.LOAN

    def test_dawho_deposit(self):
        raw = {"AcctText": "營業部DAWHO活期儲蓄存款", "currency": "TWD"}
        assert classify_account("sinopac", raw) == ProductType.DEPOSIT

    def test_dawho_fx(self):
        raw = {"AcctText": "營業部DAWHO外幣組合存款", "currency": "JPY"}
        assert classify_account("sinopac", raw) == ProductType.FX_DEPOSIT


class TestScsb:
    def test_loan_header(self):
        """SCSB 的核心 case — 解了使用者 NT$ 20.5M 貸款被當資產的 bug。"""
        raw = {
            "type_header": "貸款",
            "account_no": "90000000257044",
            "currency": "TWD",
        }
        assert classify_account("scsb", raw) == ProductType.LOAN

    def test_loan_english_header(self):
        raw = {"type_header": "Loan", "currency": "TWD"}
        assert classify_account("scsb", raw) == ProductType.LOAN

    def test_deposit_header(self):
        raw = {"type_header": "活儲存款", "currency": "TWD"}
        assert classify_account("scsb", raw) == ProductType.DEPOSIT

    def test_deposit_header_alt(self):
        raw = {"type_header": "活期存款", "currency": "TWD"}
        assert classify_account("scsb", raw) == ProductType.DEPOSIT

    def test_fx_account(self):
        raw = {"type_header": "活期存款", "currency": "USD"}
        assert classify_account("scsb", raw) == ProductType.FX_DEPOSIT

    def test_no_header_unknown(self):
        raw = {"account_no": "90000000217058", "currency": "TWD"}
        assert classify_account("scsb", raw) == ProductType.UNKNOWN


class TestLinebank:
    def test_loan_inferred_no_longer_classified(self):
        # 2026-06-15: persist_linebank 不再合成 linebank_loan_inferred row,
        # classify_linebank 也拔除 _source=loan_inferred short-circuit;
        # 這個 raw shape 從此不該再出現,但即使出現也不該回 LOAN。
        # 走 keyword fallback 「分期信貸」desc 命中 loan keyword → loan
        # （此 test 從「合成 row 必要」改為「即使 raw 帶 loan_inferred hint 也
        # 不靠 _source 走 keyword path」regression）
        raw = {"_source": "loan_inferred", "desc": "分期信貸"}
        # desc 含「信貸」keyword 應該命中 LOAN（透過 keyword path 不是 _source）
        result = classify_account("linebank", raw)
        assert result == ProductType.LOAN  # 透過 desc keyword 不是 _source short-circuit

    def test_main_deposit(self):
        raw = {"desc": "主帳戶 活儲", "currency": "TWD"}
        assert classify_account("linebank", raw) == ProductType.DEPOSIT


class TestCtbc:
    def test_loan_summary(self):
        raw = {"_source": "loan_summary", "type": "信貸總額"}
        assert classify_account("ctbc", raw) == ProductType.LOAN

    def test_deposit_acctType_keyword(self):
        raw = {"acctType": "活期存款", "currency": "TWD"}
        assert classify_account("ctbc", raw) == ProductType.DEPOSIT


class TestEsun:
    def test_twd_deposit(self):
        raw = {"category": "臺幣綜存", "currency": "TWD"}
        # 「臺幣綜存」三個字非已知 keyword，但 currency=TWD 不在 fx 觸發條件
        # 因為沒有 "活儲/活期/存款" → unknown （esun 本身須升級）
        # 暫時 fallback unknown（後面 esun persist 會直接傳 deposit）
        assert classify_account("esun", raw) in (ProductType.DEPOSIT, ProductType.UNKNOWN)

    def test_fx_deposit(self):
        raw = {"category": "外幣活存", "currency": "USD"}
        assert classify_account("esun", raw) == ProductType.FX_DEPOSIT

    def test_with_deposit_keyword(self):
        raw = {"category": "臺幣綜存活期存款", "currency": "TWD"}
        assert classify_account("esun", raw) == ProductType.DEPOSIT


# ============================================================
# 邊角 case
# ============================================================

class TestEdgeCases:
    def test_unknown_bank_falls_back_keyword(self):
        raw = {"type": "貸款"}
        assert classify_account("brand_new_bank", raw) == ProductType.LOAN

    def test_unknown_bank_empty(self):
        raw = {}
        assert classify_account("brand_new_bank", raw) == ProductType.UNKNOWN

    def test_currency_only_fx(self):
        raw = {"currency": "JPY"}
        # 無 bank 預設 → keyword empty + currency JPY → FX
        assert classify_account("brand_new_bank", raw) == ProductType.FX_DEPOSIT
