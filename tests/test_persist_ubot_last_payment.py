"""聯邦 (UBOT) persist 信用卡 last_payment / bill_due 三欄抓取 regression.

2026-06-22 (使用者指示「聯邦的信用卡繳款紀錄查詢是不是沒做」):
IBKF010001 card_limit raw 本來就有 lastPayAmt / lastPayDate / payAmt 三欄,
之前 persist 只用 crLmt/unsettleAmt/dueDate 三欄漏抓 → 聯邦永遠不會推
「new_payment」通知 + UI bill_due_amount 永遠 NULL.

修法不需開新 collector path, 純 persist mapping 補三欄. 整戶層 aggregate 套到每張卡
(UBOT 多卡共用唯一 dueDate / 唯一 lastPay 歷史 by-design).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 加入專案根目錄到 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.banks.ubot import _ubot_card_bill_fact
from backend.core.persist import persist_ubot
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    s = BankStore("ubot", user_id=1)
    yield s
    s.close()


def _base_data(card_no: str = "9000000000387027",
               last_pay_amt: str = "0",
               last_pay_date: str = "00000000",
               pay_amt: str = "38647") -> dict:
    """模擬 backend/data/ubot_collected.json 結構 (簡化版)."""
    return {
        "deposit_twd": {"TotalData": {"Deposit": "100000"}, "NTList": [], "LoanList": []},
        "deposit_foreign": {"FTList": []},
        "card_summary": {
            "CardList": [{
                "avalCrLmt": "344282", "dueDate": "20260618",
                "payAmt": pay_amt, "minAmt": pay_amt, "CTDpayAmt": "0",
            }],
            "TotalData": {"Unpaid": "41065", "Card": pay_amt},
        },
        "card_limit": {
            "CardList": [{
                "crLmt": "300000", "dueDate": "20260618",
                "payAmt": pay_amt, "minAmt": pay_amt,
                "lastPayAmt": last_pay_amt, "lastPayDate": last_pay_date,
                "CTDpayAmt": "0", "avalCrLmt": "344282", "unsettleAmt": "41065",
                "paymentAcctno": "9000000000397056", "hasBill": "Y",
            }],
        },
        "card_billed": [{
            "CardHeader": {
                "prevBal": "40956", "currBal": pay_amt, "dueAmt": pay_amt,
                "dueDate": "20260618", "stmtDate": "20260603",
            },
            "CardList": [{
                "cardNo": card_no, "seqNo": "0001",
                "effectDate": "20260515", "postDate": "20260515",
                "txCode": "43", "txAmt": "-15", "Currency": "",
                "oriAmt": "", "txDesc": "刷卡現金回饋",
                "typeName": "聯邦悠遊吉鶴卡",
            }],
        }],
        "card_unbilled": {"CardSum": "0", "DispStmtAmt": "0", "CardList": []},
        "investment": None,
        "twd_txns": [],
    }


def test_ubot_collector_treats_pay_amt_as_current_due_without_double_subtract():
    data = _base_data(pay_amt="1000")
    data["card_limit"]["CardList"][0]["CTDpayAmt"] = "400"

    fact = _ubot_card_bill_fact(data)

    assert fact is not None
    assert fact["remaining_due"] == 1000.0


def test_ubot_sentinel_payment_pair_is_omitted_not_fatal():
    fact = _ubot_card_bill_fact(_base_data())

    assert fact is not None
    assert "last_payment_amount" not in fact
    assert "last_payment_date" not in fact


def _read_card(store: BankStore, card_no: str) -> dict | None:
    cur = store.conn.execute(
        "SELECT card_no, bill_due_amount, last_payment_amount, last_payment_date "
        "FROM cards WHERE user_id=? AND card_no=?",
        (store.user_id, card_no),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "card_no": row[0],
        "bill_due_amount": row[1],
        "last_payment_amount": row[2],
        "last_payment_date": row[3],
    }


def test_persist_writes_bill_due_amount_from_payAmt(store):
    """payAmt 38647 → cards.bill_due_amount = 38647.0 (新通則)."""
    persist_ubot(_base_data(pay_amt="38647"), store, rules=None)
    card = _read_card(store, "9000000000387027")
    assert card is not None
    assert card["bill_due_amount"] == pytest.approx(38647.0)


def test_persist_writes_last_payment_when_present(store):
    """lastPayAmt=12345 + lastPayDate=20260520 → 真實有繳款."""
    data = _base_data(last_pay_amt="12345", last_pay_date="20260520")
    persist_ubot(data, store, rules=None)
    card = _read_card(store, "9000000000387027")
    assert card is not None
    assert card["last_payment_amount"] == pytest.approx(12345.0)
    assert card["last_payment_date"] == "2026-05-20"


def test_persist_zero_pay_with_sentinel_date_kept_as_zero(store):
    """lastPayAmt=0 + lastPayDate=00000000 → amount 寫 0, date 寫 None.

    2026-06-22 v2: 不再把 amount 0 sentinel 成 None. 0 是合法值
    (聯邦自動扣繳尚未到期). card_events 仍因 last_payment_date is None 不推通知.
    """
    data = _base_data(last_pay_amt="0", last_pay_date="00000000")
    persist_ubot(data, store, rules=None)
    card = _read_card(store, "9000000000387027")
    assert card is not None
    assert card["last_payment_amount"] == 0.0, "0 是合法值, UI 該看到「本期已繳 0」"
    assert card["last_payment_date"] is None, "00000000 sentinel 必須轉 None"


def test_persist_multi_card_share_same_last_payment(store):
    """UBOT 整戶層 by-design: 多卡共用同一組 lastPay 歷史 (整戶聚合扣繳)."""
    data = _base_data(last_pay_amt="5000", last_pay_date="20260601")
    # 加第二張卡到 billed
    data["card_billed"][0]["CardList"].append({
        "cardNo": "9000000000407057", "seqNo": "0001",
        "effectDate": "20260510", "postDate": "20260510",
        "txCode": "40", "txAmt": "1000", "Currency": "",
        "oriAmt": "", "txDesc": "test", "typeName": "微風VISA無限卡",
    })
    persist_ubot(data, store, rules=None)
    card1 = _read_card(store, "9000000000387027")
    card2 = _read_card(store, "9000000000407057")
    assert card1 is not None and card2 is not None
    assert card1["last_payment_amount"] == pytest.approx(5000.0)
    assert card2["last_payment_amount"] == pytest.approx(5000.0), \
        "整戶層 by-design 多卡共用 lastPay"
    assert card1["last_payment_date"] == card2["last_payment_date"] == "2026-06-01"


def test_persist_refreshes_shared_used_credit_for_cards_absent_from_latest_activity(store):
    """整戶額度須更新所有既有卡，且 used = credit limit - available limit。"""
    store.upsert_cards([
        {
            "number": "9000000000387027", "name": "聯邦悠遊吉鶴卡",
            "used_credit": 45814, "active": False,
        },
        {"number": "9000000000407057", "name": "舊卡名", "used_credit": 86918},
    ])
    data = _base_data(card_no="9000000000407057")
    data["card_limit"]["CardList"][0].update({
        "crLmt": "300000",
        "avalCrLmt": "299874",
        "unsettleAmt": "86918",
    })

    persist_ubot(data, store, rules=None)

    rows = store.conn.execute(
        "SELECT card_no, name, used_credit, active FROM cards "
        "WHERE user_id=? ORDER BY card_no",
        (store.user_id,),
    ).fetchall()
    assert [
        (row["card_no"], row["name"], row["used_credit"], row["active"])
        for row in rows
    ] == [
        ("9000000000387027", "聯邦悠遊吉鶴卡", 126.0, 0),
        ("9000000000407057", "聯邦悠遊吉鶴卡", 126.0, 1),
    ]


def test_persist_shared_metadata_is_user_scoped(store):
    """整戶額度更新不得碰同銀行其他 user 的卡。"""
    other = BankStore("ubot", user_id=2)
    other.upsert_cards([{
        "number": "9000000000999999",
        "name": "其他使用者聯邦卡",
        "used_credit": 777,
    }])
    other.close()

    data = _base_data()
    data["card_limit"]["CardList"][0].update({
        "crLmt": "300000", "avalCrLmt": "299874",
    })
    persist_ubot(data, store, rules=None)

    other = BankStore("ubot", user_id=2)
    row = other.conn.execute(
        "SELECT used_credit FROM cards WHERE user_id=?",
        (2,),
    ).fetchone()
    other.close()
    assert row is not None
    assert row["used_credit"] == 777.0


@pytest.mark.parametrize(
    ("limit_available", "summary_available", "expected_used"),
    [
        ("", "299874", 126.0),
        ("not-a-number", "299874", 126.0),
        ("0", "299874", 300000.0),
        ("344282", "299874", -44282.0),
        (None, None, 86918.0),
    ],
)
def test_persist_available_limit_falls_back_after_parse_failure(
    store, limit_available, summary_available, expected_used,
):
    """可用額度依解析結果 fallback，保留合法零與負數。"""
    data = _base_data()
    data["card_limit"]["CardList"][0].update({
        "crLmt": "300000",
        "avalCrLmt": limit_available,
        "unsettleAmt": "86918",
    })
    data["card_summary"]["CardList"][0]["avalCrLmt"] = summary_available

    persist_ubot(data, store, rules=None)

    row = store.conn.execute(
        "SELECT used_credit FROM cards WHERE user_id=?",
        (store.user_id,),
    ).fetchone()
    assert row["used_credit"] == expected_used


def test_persist_card_only_in_unbilled_also_gets_metadata(store):
    """只出現在 card_unbilled (沒 billed) 的卡也該套整戶 metadata."""
    data = _base_data(last_pay_amt="5000", last_pay_date="20260601")
    # 清空 billed, 卡只在 unbilled
    data["card_billed"] = []
    data["card_unbilled"] = {
        "CardSum": "0", "DispStmtAmt": "0",
        "CardList": [{
            "cardNo": "9000000000407057", "seq": "00001",
            "effectiveDate": "20260609", "postingDate": "20260610",
            "txCode": "40", "txAmt": "29", "oriCode": "",
            "oriAmt": "0.00", "txDesc": "微風信義",
            "typeName": "微風VISA無限卡",
        }],
    }
    persist_ubot(data, store, rules=None)
    card = _read_card(store, "9000000000407057")
    assert card is not None
    assert card["last_payment_amount"] == pytest.approx(5000.0)
    assert card["last_payment_date"] == "2026-06-01"
    assert card["bill_due_amount"] == pytest.approx(38647.0)


def test_persist_missing_card_limit_graceful(store):
    """card_limit None → 三欄全 None, 不 raise."""
    data = _base_data()
    data["card_limit"] = None
    persist_ubot(data, store, rules=None)
    card = _read_card(store, "9000000000387027")
    assert card is not None
    # card_summary 還有 payAmt 可 fallback
    assert card["bill_due_amount"] == pytest.approx(38647.0)
    # last_payment_* 只在 card_limit 有, 沒 fallback 來源
    assert card["last_payment_amount"] is None
    assert card["last_payment_date"] is None


# ============================================================
# 2026-06-22 v3: F0801001 IBKF080001 近期繳款紀錄 (使用者指出 ubot 有此 page)
# ============================================================

def test_f0801001_pay_list_overrides_card_limit_lastpay(store):
    """F0801001 payList 有最新繳款日 → 覆寫 card_limit 的 lastPayDate=00000000."""
    data = _base_data(last_pay_amt="0", last_pay_date="00000000")  # card_limit 沒繳款
    data["card_pay_history"] = {
        "PayList": [
            {"payDate": "20260510", "payAmt": "1500", "cardNo": "9000000000387027"},
            {"payDate": "20260605", "payAmt": "2380", "cardNo": "9000000000387027"},  # 最新
            {"payDate": "20260415", "payAmt": "800", "cardNo": "9000000000387027"},
        ],
    }
    persist_ubot(data, store, rules=None)
    card = _read_card(store, "9000000000387027")
    assert card is not None
    assert card["last_payment_amount"] == 2380.0, "F0801001 應該覆寫 card_limit 的 0"
    assert card["last_payment_date"] == "2026-06-05", "最新一筆 payDate"


def test_f0801001_alternative_key_names_payList(store):
    """raw shape 未確認, 試 payList (lowercase p) 也該認."""
    data = _base_data(last_pay_amt="0", last_pay_date="00000000")
    data["card_pay_history"] = {
        "payList": [
            {"PayDate": "20260601", "amount": "5000", "cardNo": "9000000000387027"},
        ],
    }
    persist_ubot(data, store, rules=None)
    card = _read_card(store, "9000000000387027")
    assert card is not None
    assert card["last_payment_amount"] == 5000.0
    assert card["last_payment_date"] == "2026-06-01"


def test_f0801001_empty_or_missing_graceful(store):
    """F0801001 抓不到 (None / empty list) → 不 raise, 保持 card_limit 既有值."""
    data = _base_data(last_pay_amt="3000", last_pay_date="20260520")  # card_limit 有值
    data["card_pay_history"] = None  # F0801001 沒抓到
    persist_ubot(data, store, rules=None)
    card = _read_card(store, "9000000000387027")
    assert card is not None
    # 保持 card_limit 既有值
    assert card["last_payment_amount"] == 3000.0
    assert card["last_payment_date"] == "2026-05-20"


def test_f0801001_unknown_shape_graceful_skip(store):
    """F0801001 raw shape 全不認識 (沒任何已知 key) → silent skip."""
    data = _base_data(last_pay_amt="3000", last_pay_date="20260520")
    data["card_pay_history"] = {"UnknownKey": [{"weird": "shape"}]}
    persist_ubot(data, store, rules=None)
    card = _read_card(store, "9000000000387027")
    assert card is not None
    # 保持 card_limit 既有值, 不 raise
    assert card["last_payment_amount"] == 3000.0
    assert card["last_payment_date"] == "2026-05-20"


def test_f0801001_real_datelist_shape(store):
    """0.3.33: 真實 F0801001 raw shape — DateList + YYYY/MM/DD + 逗號金額.

    從 2026-06-22 local crawl 抓到的真實 raw:
      {"DateList": [{"postDate": "2026/06/22", "effectDate": "2026/06/22",
                     "payAmt": "38,647", "txDesc": "自動轉帳－聯邦銀行",
                     "seqNo": "00001"}, ...]}
    """
    data = _base_data(last_pay_amt="0", last_pay_date="00000000")  # card_limit 沒繳款
    data["card_pay_history"] = {
        "DateList": [
            {"postDate": "2026/06/22", "effectDate": "2026/06/22",
             "payAmt": "38,647", "txDesc": "自動轉帳－聯邦銀行", "seqNo": "00001"},
            {"postDate": "2026/05/19", "effectDate": "2026/05/19",
             "payAmt": "40,956", "txDesc": "自動轉帳－聯邦銀行", "seqNo": "00002"},
            {"postDate": "2026/04/21", "effectDate": "2026/04/21",
             "payAmt": "61,727", "txDesc": "自動轉帳－聯邦銀行", "seqNo": "00003"},
        ],
    }
    persist_ubot(data, store, rules=None)
    card = _read_card(store, "9000000000387027")
    assert card is not None
    # 最新一筆 (postDate 排序最大) = 2026/06/22 / 38,647
    assert card["last_payment_amount"] == 38647.0, "逗號數字 '38,647' 該解成 38647.0"
    assert card["last_payment_date"] == "2026-06-22", "YYYY/MM/DD 該轉 YYYY-MM-DD"


def test_ubot_unbilled_preserves_original_currency_amount(store):
    """聯邦 pending 必須保留 Currency/oriAmt，供結匯後用原幣 identity 配對。"""
    data = _base_data()
    data["card_unbilled"] = {
        "CardSum": "3200",
        "DispStmtAmt": "3200",
        "CardList": [{
            "cardNo": "****1234", "effectiveDate": "20260701",
            "txDesc": "AMAZON JP", "txAmt": "3200",
            "Currency": "USD", "oriAmt": "100.20", "txCode": "",
        }],
    }
    persist_ubot(data, store, rules=None)

    row = store.conn.execute(
        "SELECT currency, consume_currency, consume_amount FROM card_pending_txns"
    ).fetchone()
    assert row["currency"] == "TWD"
    assert row["consume_currency"] == "USD"
    assert row["consume_amount"] == 100.2
