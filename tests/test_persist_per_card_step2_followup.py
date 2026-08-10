"""驗證 persist_taishin 和 persist_sinopac 處理 per-card limit/used/due/stmt。

2026-06-14 Step 2 follow-up:
- Taishin: doXTPA.value.001 OUT-CRLIMIT-PERM + OUT-AVAIL-CREDIT 算 used_credit
- Sinopac: card_statements[0] 套 payment_due_date + billing_cycle_date 到 cards
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.persist import persist_sinopac, persist_taishin
from backend.core.store import BankStore


@pytest.fixture
def store_taishin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    s = BankStore("taishin_test")
    yield s
    s.close()


@pytest.fixture
def store_sinopac(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    s = BankStore("sinopac_test")
    yield s
    s.close()


def test_taishin_used_credit_computed_from_doxtpa(store_taishin):
    """doXTPA.value.001 OUT-CRLIMIT-PERM - OUT-AVAIL-CREDIT = 即時已動用額度。"""
    data = {
        "api_responses": {
            "doXTPA": {
                "value": {
                    "001": {
                        "OUT-CRLIMIT-PERM": "000400000",  # 400,000
                        "OUT-AVAIL-CREDIT": " 000399920",  # 399,920 (即時可用)
                    },
                },
            },
            "qryRealTime": {"value": {"crlimit": "400,000"}},
            "query": {"OUTPUTDATA": {"SavingAccount": []}},
        },
        "credit_card_parsed": {
            "cards": [{"number": "****7018", "name": "Richart卡"}],
            "billing_period": {
                "pay_due_date": "2026/05/27",
                "statement_date": "2026/5/12",
            },
            "top_summary": {"unpaid": 0},
        },
    }
    persist_taishin(data, store_taishin)
    rows = list(store_taishin.conn.execute(
        "SELECT card_no, credit_limit, used_credit FROM cards"
    ))
    assert len(rows) == 1
    assert rows[0][0] == "****7018"
    assert rows[0][1] == 400000.0  # credit_limit
    # used_credit = 400000 - 399920 = 80 (即時已動用，含未出帳)
    # 比 top_summary.unpaid=0 (僅未繳已出帳) 更準確
    assert rows[0][2] == 80.0


def test_taishin_fallback_to_top_summary_when_doxtpa_missing(store_taishin):
    """doXTPA 缺時 fallback 用 top_summary.unpaid。"""
    data = {
        "api_responses": {
            "qryRealTime": {"value": {"crlimit": "400,000"}},
            "query": {"OUTPUTDATA": {"SavingAccount": []}},
        },
        "credit_card_parsed": {
            "cards": [{"number": "****7018", "name": "Richart卡"}],
            "billing_period": {"pay_due_date": "2026/05/27"},
            "top_summary": {"unpaid": 12345},
        },
    }
    persist_taishin(data, store_taishin)
    rows = list(store_taishin.conn.execute(
        "SELECT used_credit FROM cards"
    ))
    assert rows[0][0] == 12345.0  # top_summary.unpaid fallback


def test_taishin_persists_twd_transaction_detail_rows(store_taishin):
    """RB0102 查詢交易明細結果必須寫入 twd_transactions."""
    data = {
        "api_responses": {
            "query": {"OUTPUTDATA": {"SavingAccount": []}},
        },
        "twd_txn_results": [
            {
                "selected_text": "9000-00-0022703-1 測試帳戶",
                "query_result": {
                    "accountText": "9000-00-0022703-1 測試帳戶",
                    "periodText": "1個月",
                },
                "text": (
                    "查詢結果\n交易明細\n依排序\n"
                    "交易日\n\t\n帳務日\n\t\n摘要\n\t\n金額\n\t\n餘額\n\t\n備註\n\t\n\n\n"
                    "2026/06/29 16:16:16\n\t\n2026/06/29\n\t\n媒體轉帳\n\t\n-80\n\t\n0\n\t\n測試卡費\n\t\n消費屬性設定\n\n\n"
                    "2026/06/26 16:53:41\n\t\n2026/06/26\n\t\nCD轉入\n\t\n80\n\t\n80\n\t\nATM 807-0090000000197014\n\t\n消費屬性設定\n\n\n"
                    "共 2 筆資料資料日期：2026/06/30 22:17:28\n沒有更多資料了\n"
                ),
            }
        ],
    }

    delta = persist_taishin(data, store_taishin)

    assert delta["twd_txn_new"] == 2
    rows = [
        tuple(row)
        for row in store_taishin.conn.execute(
            "SELECT account_no, txn_datetime, account_date, description, raw_description, "
            "expend, income, balance, memo "
            "FROM twd_transactions ORDER BY txn_datetime"
        )
    ]
    assert rows == [
        ("90000000227031", "2026-06-26 16:53:41", "2026-06-26",
         "CD轉入 - ATM 807-0090000000197014", "CD轉入", None, 80.0, 80.0,
         "ATM 807-0090000000197014"),
        ("90000000227031", "2026-06-29 16:16:16", "2026-06-29",
         "媒體轉帳 - 測試卡費", "媒體轉帳", 80.0, None, 0.0, "測試卡費"),
    ]


def test_sinopac_applies_due_and_stmt_from_card_statements(store_sinopac):
    """card_statements[最新月] 的 due/billing_cycle_date 套到每張卡。"""
    data = {
        "deposit": [],
        "all_cards": {
            "Result": {
                "Items": [
                    {
                        "CardNo": "9000000000417020",
                        "Name": "DAWHO現金回饋Debit卡",
                        "CardTypeDesc": "Debit",
                        "CardBrand": "MasterCard",
                        "ExpDate": "0829",  # 2029/08 有效
                    },
                ],
            },
        },
        "card_statements": [
            {
                "month": "2026/05",
                "billing_cycle_date": "2026/05/17",
                "payment_due_date": "2026/06/01",
            },
            {
                "month": "2026/04",
                "billing_cycle_date": "2026/04/16",
                "payment_due_date": "2026/05/01",
            },
        ],
    }
    persist_sinopac(data, store_sinopac)
    rows = list(store_sinopac.conn.execute(
        "SELECT card_no, payment_due_date, statement_close_date FROM cards"
    ))
    assert len(rows) == 1
    assert rows[0][1] == "2026-06-01"  # 取最新月 + ISO 格式
    assert rows[0][2] == "2026-05-17"


def test_sinopac_no_statements_due_and_stmt_remain_null(store_sinopac):
    """card_statements 為空時 due/stmt 不寫（COALESCE 保留舊值）。"""
    data = {
        "deposit": [],
        "all_cards": {
            "Result": {
                "Items": [
                    {
                        "CardNo": "9000000000417020",
                        "Name": "DAWHO卡",
                        "ExpDate": "0829",
                    },
                ],
            },
        },
        "card_statements": [],
    }
    persist_sinopac(data, store_sinopac)
    rows = list(store_sinopac.conn.execute(
        "SELECT payment_due_date, statement_close_date FROM cards"
    ))
    assert rows[0][0] is None
    assert rows[0][1] is None


# ============================================================
# HSBC card_detail.details[] → credit_limit + statement_close_date
# ============================================================

@pytest.fixture
def store_hsbc(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.core.store import BankStore as BS
    s = BS("hsbc_test")
    yield s
    s.close()


def test_hsbc_credit_limit_extracted_from_details(store_hsbc):
    """card_detail[tail].detail.details[] key='Credit Limit' → cards.credit_limit。"""
    from backend.core.persist import persist_hsbc
    data = {
        "cards": [
            {"maskedCardNumber": "9059-****-****-7059", "name": "滙豐Live+卡",
             "cardType": "信用卡", "outstandingBalance": "18,198",
             "paymentDueDate": "05-06-2026", "cardStatusDisplay": "ACTIVATED"},
        ],
        "card_detail": {
            "7059": {
                "masked": "9059-****-****-7059",
                "detail": {
                    "details": [
                        {"key": "Credit Limit", "value": "1,500,000 TWD"},
                        {"key": "Available Credit Limit", "value": "1,354,324 TWD"},
                        {"key": "Last Statement Date", "value": "18 May 2026"},
                    ],
                },
            },
        },
    }
    persist_hsbc(data, store_hsbc)
    rows = list(store_hsbc.conn.execute(
        "SELECT card_no, credit_limit, statement_close_date FROM cards"
    ))
    assert len(rows) == 1
    assert rows[0][1] == 1500000.0  # Credit Limit "1,500,000 TWD"
    assert rows[0][2] == "2026-05-18"  # Last Statement Date "18 May 2026"


def test_hsbc_details_missing_fields_safe(store_hsbc):
    """details[] 沒 Credit Limit/Statement Date 時不 crash, credit_limit=None。"""
    from backend.core.persist import persist_hsbc
    data = {
        "cards": [
            {"maskedCardNumber": "9059-****-****-7059", "name": "滙豐卡",
             "cardType": "信用卡", "outstandingBalance": "0",
             "paymentDueDate": "05-06-2026", "cardStatusDisplay": "ACTIVATED"},
        ],
        "card_detail": {
            "7059": {
                "masked": "9059-****-****-7059",
                "detail": {
                    "details": [
                        {"key": "Auto Debit", "value": "Yes"},  # 不相關欄
                    ],
                },
            },
        },
    }
    persist_hsbc(data, store_hsbc)
    rows = list(store_hsbc.conn.execute("SELECT credit_limit FROM cards"))
    assert rows[0][0] is None  # 沒抓到 Credit Limit → None


def test_hsbc_no_card_detail_block_safe(store_hsbc):
    """card_detail 整段缺時不 crash, credit_limit=None。"""
    from backend.core.persist import persist_hsbc
    data = {
        "cards": [
            {"maskedCardNumber": "9059-****-****-7059", "name": "滙豐卡",
             "cardType": "信用卡", "outstandingBalance": "0",
             "paymentDueDate": "05-06-2026", "cardStatusDisplay": "ACTIVATED"},
        ],
        # card_detail 整段沒提供
    }
    persist_hsbc(data, store_hsbc)
    rows = list(store_hsbc.conn.execute("SELECT credit_limit FROM cards"))
    assert rows[0][0] is None


def test_hsbc_dmy_text_to_iso_helper():
    """'18 May 2026' → '2026-05-18'，邊界 case 安全。"""
    from backend.core.persist.hsbc import _hsbc_dmy_text_to_iso
    assert _hsbc_dmy_text_to_iso("18 May 2026") == "2026-05-18"
    assert _hsbc_dmy_text_to_iso("05 Jun 2026") == "2026-06-05"
    assert _hsbc_dmy_text_to_iso("01 Jan 2025") == "2025-01-01"
    # 邊界
    assert _hsbc_dmy_text_to_iso(None) is None
    assert _hsbc_dmy_text_to_iso("") is None
    assert _hsbc_dmy_text_to_iso("foo bar baz") is None  # 無效月份
    assert _hsbc_dmy_text_to_iso("18-May-2026") is None  # 格式不對


# ============================================================
# 2026-06-14 Step 3：CTBC credit_limit (quota+availBal) + stmt_close billCycle
# ============================================================

@pytest.fixture
def store_ctbc_step3(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    s = BankStore("ctbc_step3_test")
    yield s
    s.close()


def test_ctbc_credit_limit_is_quota_plus_availbal(store_ctbc_step3):
    """CTBC: quota=25025, availBal=674975 → credit_limit=700000, used=25025。"""
    from backend.core.persist import persist_ctbc
    data = {
        "summary": {
            "creditCardSummary": {
                "quota": 25025,
                "availBal": 674975,
                "unpaidStmt": 0,
                "pmtExpDt": "2026/07/05",
            },
        },
        "card_api_dump": {
            "/twrbc-card/qu002/010": {
                "billCycle": "17",
                "cardDataList": [
                    {"cardNoSuffixFour": "7036_0", "cardName": "中華航空",
                     "positiveOrAttached": "正卡"},
                ],
            },
        },
    }
    persist_ctbc(data, store_ctbc_step3)
    rows = list(store_ctbc_step3.conn.execute(
        "SELECT card_no, credit_limit, used_credit, statement_close_date, payment_due_date FROM cards"
    ))
    assert len(rows) >= 1
    # 第一張卡 (****7036)
    card = next(r for r in rows if "7036" in r[0])
    assert card[1] == 700000.0  # credit_limit = quota + availBal
    assert card[2] == 25025.0  # used = quota
    assert card[3] is not None  # stmt_close 從 billCycle 推算
    assert card[3].endswith("-17")  # 結帳日 17 號
    assert card[4] == "2026-07-05"  # pmtExpDt 轉 ISO


def test_ctbc_credit_limit_safe_when_missing(store_ctbc_step3):
    """CTBC: quota/availBal 缺一不 crash, credit_limit=None。"""
    from backend.core.persist import persist_ctbc
    data = {
        "summary": {
            "creditCardSummary": {
                # quota missing
                "availBal": 100000,
                "pmtExpDt": "2026/07/05",
            },
        },
        "card_api_dump": {
            "/twrbc-card/qu002/010": {
                "cardDataList": [
                    {"cardNoSuffixFour": "1234_0", "cardName": "測試卡"},
                ],
            },
        },
    }
    persist_ctbc(data, store_ctbc_step3)
    rows = list(store_ctbc_step3.conn.execute(
        "SELECT credit_limit, used_credit FROM cards"
    ))
    assert rows[0][0] is None  # quota 缺 → limit=None
    assert rows[0][1] is None  # quota 缺 → used=None


def test_bill_cycle_to_latest_stmt_date_helper():
    """billCycle 推算最近結帳日: today.day >= cycle → 本月; today.day < cycle → 上月。"""
    from datetime import datetime as _dt
    from backend.core.persist.ctbc import _bill_cycle_to_latest_stmt_date

    # today=6/13 cycle=17 → 上月(5/17) — 今天 13 還沒到 17
    assert _bill_cycle_to_latest_stmt_date("17", _dt(2026, 6, 13)) == "2026-05-17"
    # today=6/20 cycle=17 → 本月(6/17) — 今天 20 已過 17
    assert _bill_cycle_to_latest_stmt_date("17", _dt(2026, 6, 20)) == "2026-06-17"
    # today=1/5 cycle=17 → 上月(去年12/17)
    assert _bill_cycle_to_latest_stmt_date("17", _dt(2026, 1, 5)) == "2025-12-17"
    # 整數也 OK
    assert _bill_cycle_to_latest_stmt_date(17, _dt(2026, 6, 13)) == "2026-05-17"
    # 邊界
    assert _bill_cycle_to_latest_stmt_date(None) is None
    assert _bill_cycle_to_latest_stmt_date("") is None
    assert _bill_cycle_to_latest_stmt_date("abc") is None  # 非數字
    assert _bill_cycle_to_latest_stmt_date("32") is None  # 超出 1-31
    assert _bill_cycle_to_latest_stmt_date("0") is None  # 0 也不合法


# ============================================================
# 2026-06-14 Step 3：ESun used_credit (已入帳累加) + stmt_close (due-30d)
# ============================================================

@pytest.fixture
def store_esun_step3(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    s = BankStore("esun_step3_test")
    yield s
    s.close()


def test_esun_used_credit_sum_billed(store_esun_step3):
    """ESun: card_transactions 已入帳 billed_amount 加總 = used_credit。"""
    from backend.core.persist import persist_esun
    data = {
        "accounts": [],
        "card_summary": {
            "credit_limit_twd": 400000,
            "payment_due_date_roc": "115/07/29",  # 2026-07-29
        },
        "card_transactions": [
            {"card_last4": "7032", "card_no": "9064-XXXX-XXXX-7032",
             "billed_amount": 358.0, "status": "已入帳"},
            {"card_last4": "7032", "card_no": "9064-XXXX-XXXX-7032",
             "billed_amount": 1727.0, "status": "已入帳"},
            {"card_last4": "7032", "card_no": "9064-XXXX-XXXX-7032",
             "billed_amount": 999.0, "status": "未入帳"},  # 不算
        ],
    }
    persist_esun(data, store_esun_step3)
    rows = list(store_esun_step3.conn.execute(
        "SELECT card_no, credit_limit, used_credit, statement_close_date, payment_due_date FROM cards"
    ))
    assert len(rows) == 1
    assert rows[0][1] == 400000.0
    assert rows[0][2] == 2085.0  # 358 + 1727 (只算「已入帳」)
    assert rows[0][3] == "2026-06-29"  # 7/29 - 30天 = 6/29
    assert rows[0][4] == "2026-07-29"


def test_esun_used_credit_none_when_no_billed(store_esun_step3):
    """ESun: 沒「已入帳」交易 → used_credit=None (不寫 0 免誤導)。"""
    from backend.core.persist import persist_esun
    data = {
        "accounts": [],
        "card_summary": {"credit_limit_twd": 400000, "payment_due_date_roc": "115/07/29"},
        "card_transactions": [
            {"card_last4": "7032", "card_no": "9064-XXXX-XXXX-7032",
             "billed_amount": 999.0, "status": "未入帳"},
        ],
    }
    persist_esun(data, store_esun_step3)
    rows = list(store_esun_step3.conn.execute(
        "SELECT used_credit FROM cards"
    ))
    assert rows[0][0] is None


# ============================================================
# 2026-06-14 Step 3：Sinopac card_summary 抽 limit/used
# ============================================================

def test_sinopac_credit_limit_from_card_summary(store_sinopac):
    """Sinopac: card_summary[0].SubInfo 攤平 dict → 抓「信用額度(臺幣)」+「以刷卡未請款金額」。"""
    data = {
        "all_cards": {
            "Result": {"Items": [
                {"CardNo": "9000000000417020", "Name": "DAWHO Debit",
                 "CardTypeDesc": "主卡", "CardBrand": "", "ExpDate": "0829"},
            ]},
        },
        "card_summary": [
            {
                "TitleInfo": "",
                "SubInfo": [
                    [
                        {"DataText": "信用額度(臺幣)", "DataValue": "409,000"},
                        {"DataText": "以刷卡未請款金額", "DataValue": "1,234"},
                        {"DataText": "結帳日", "DataValue": "2026/05/17"},
                        {"DataText": "繳款截止日", "DataValue": "2026/06/01"},
                    ],
                ],
            },
        ],
    }
    persist_sinopac(data, store_sinopac)
    rows = list(store_sinopac.conn.execute(
        "SELECT card_no, credit_limit, used_credit, statement_close_date, payment_due_date FROM cards"
    ))
    assert len(rows) == 1
    assert rows[0][1] == 409000.0
    assert rows[0][2] == 1234.0
    assert rows[0][3] == "2026-05-17"
    assert rows[0][4] == "2026-06-01"


def test_sinopac_card_summary_fallback_to_due_when_no_unbilled(store_sinopac):
    """Sinopac: 沒「以刷卡未請款金額」→ fallback 用「本期應繳」。"""
    data = {
        "all_cards": {
            "Result": {"Items": [
                {"CardNo": "9000000000417020", "Name": "DAWHO", "ExpDate": "0829"},
            ]},
        },
        "card_summary": [
            {
                "TitleInfo": "",
                "SubInfo": [
                    [
                        {"DataText": "信用額度(臺幣)", "DataValue": "100,000"},
                        # 沒「以刷卡未請款金額」
                        {"DataText": "本期應繳", "DataValue": "5,000"},
                    ],
                ],
            },
        ],
    }
    persist_sinopac(data, store_sinopac)
    rows = list(store_sinopac.conn.execute("SELECT used_credit FROM cards"))
    assert rows[0][0] == 5000.0  # fallback 到本期應繳


def test_sinopac_card_summary_safe_when_empty(store_sinopac):
    """Sinopac: card_summary 整段缺時不 crash。"""
    data = {
        "all_cards": {
            "Result": {"Items": [
                {"CardNo": "9000000000417020", "Name": "DAWHO", "ExpDate": "0829"},
            ]},
        },
        # card_summary 整段沒提供
    }
    persist_sinopac(data, store_sinopac)
    rows = list(store_sinopac.conn.execute(
        "SELECT credit_limit, used_credit FROM cards"
    ))
    assert rows[0][0] is None
    assert rows[0][1] is None
