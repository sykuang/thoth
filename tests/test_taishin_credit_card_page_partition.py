# ruff: noqa: W293 — triple-quoted fixtures intentionally preserve whitespace-only rows.
"""Regression tests for taishin _parse_credit_card_page section partition.

2026-07-03 (0.3.64): 台新繳款頁多個 sections 用同名 header
  上期實繳金額明細 → 當期消費明細 → 當期帳單說明
舊 regex 用「消費金額」當 anchor + 「沒有更多資料了」收尾，當「上期實繳
金額明細」= 「查無資料」時貪婪抓到下方「當期消費明細」rows，把當期消費
（如捷運 40 元）誤當扣繳紀錄。

新做法：改用 text.split() 做 section partition，只在「上期實繳金額明細」到
「當期消費明細」中間 block 內抓 rows。若該 block 含「查無資料」直接視為空。

實測 6 個月頁面：
  06 月 → 空 (上期實繳查無資料)
  05 月 → 4/27 -56
  04 月 → 空 (上期實繳查無資料)
  03 月 → 3/09 -1466
  02 月 → 1/27 -7173
  01 月 → 2025/12/29 -3285
"""
from __future__ import annotations

from backend.banks.taishin import TaishinCrawler


PAGE_WITH_PAID = """\
上期實繳金額明細
依排序
消費日期
	
入帳起息日
	
消費明細(含消費地)
	
約定幣別
	
消費金額
	
外幣折算日
	
消費地
	
外幣幣別/金額


2026/04/27
	
2026/04/27
	
台新銀行帳戶自動轉帳扣繳台新信用卡款
	
新臺幣
	
-56
	
	
	
沒有更多資料了
當期消費明細
幣別：
全部
查無資料
"""


PAGE_WITH_PAID_EMPTY_AND_CURRENT_TXNS = """\
上期實繳金額明細
依排序
查無資料
當期消費明細
幣別：
全部
Richart卡(原FlyGo鈦金商務) (卡號末四碼:1409)
依消費日期排序
消費日期
	
入帳起息日
	
消費明細(含消費地)
	
約定幣別
	
消費金額
	
外幣折算日
	
消費地
	
外幣幣別/金額


2026/05/21
	
2026/05/22
	
臺北大眾捷運股份有限公司A9149 TAIPEI
	
新臺幣
	
40
	
	
TW
	

2026/05/17
	
2026/05/18
	
臺北大眾捷運股份有限公司A9149 TAIPEI
	
新臺幣
	
40
	
	
TW
	

消費筆數：2 / 消費金額：TWD 80
沒有更多資料了
"""


PAGE_WITH_PAID_AND_REFUND_IN_CURRENT = """\
上期實繳金額明細
依排序
消費日期
	
入帳起息日
	
消費明細(含消費地)
	
約定幣別
	
消費金額


2026/03/09
	
2026/03/09
	
台新銀行帳戶自動轉帳扣繳台新信用卡款
	
新臺幣
	
-1,466
	
	
	
沒有更多資料了
當期消費明細
幣別：
全部
Richart卡(原FlyGo鈦金商務) (卡號末四碼:1409)
依消費日期排序


2026/02/25
	
2026/03/04
	
一卡通餘額退款
	
新臺幣
	
-204
	
	
TW
	

沒有更多資料了
"""


def test_parse_returns_paid_row_when_paid_section_has_data():
    # 用 __new__ 繞開 TaishinCrawler.__init__ 的 TaishinCreds.load()
    # (CI 環境沒 bank creds env; _parse_credit_card_page 是 pure method).
    parsed = TaishinCrawler.__new__(TaishinCrawler)._parse_credit_card_page(PAGE_WITH_PAID)
    rows = parsed["billed_txns"]
    assert len(rows) == 1
    assert rows[0]["post_date"] == "2026/04/27"
    assert rows[0]["amount"] == -56.0
    assert "扣繳" in rows[0]["desc"]


def test_parse_returns_empty_when_paid_section_says_查無資料_even_if_current_has_txns():
    """關鍵 regression: 06 月和 04 月頁上期實繳「查無資料」但當期消費有 rows，
    parser 不能把當期消費 rows 誤當扣繳紀錄。"""
    parsed = TaishinCrawler.__new__(TaishinCrawler)._parse_credit_card_page(
        PAGE_WITH_PAID_EMPTY_AND_CURRENT_TXNS
    )
    # billed_txns 應該完全空 → 不會把當期捷運 40 元誤當繳款
    assert parsed["billed_txns"] == [], (
        f"expected empty billed_txns, got {parsed['billed_txns']}"
    )


def test_parse_does_not_include_current_refund_row_as_payment():
    """3 月頁真扣繳 -1466 應被抓；當期消費「一卡通餘額退款」-204 不能誤入。"""
    parsed = TaishinCrawler.__new__(TaishinCrawler)._parse_credit_card_page(
        PAGE_WITH_PAID_AND_REFUND_IN_CURRENT
    )
    rows = parsed["billed_txns"]
    assert len(rows) == 1
    assert rows[0]["amount"] == -1466.0
    assert "扣繳" in rows[0]["desc"]
    # 不含「一卡通餘額退款」
    assert not any("退款" in (r.get("desc") or "") for r in rows)


def test_parse_returns_empty_when_no_paid_section_header_at_all():
    parsed = TaishinCrawler.__new__(TaishinCrawler)._parse_credit_card_page("完全沒有相關字樣")
    assert parsed["billed_txns"] == []
