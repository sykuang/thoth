"""Live 2026-10-03: deposit accounts are read from rendered overview pages."""
from backend.banks.esun import EsunCrawler
from backend.banks.scb import ScbCrawler
from backend.banks.taishin import TaishinCrawler


def test_esun_overview_accepts_spa_single_line_and_legacy_layout():
    spa = "交易明細\n臺幣\n外幣\n0123456789012 臺幣綜存\n臺幣帳戶總額(TWD)\n1,234\n"
    fx = "0123456789013 外幣活存\nUSD 5.00\n"
    legacy = "臺幣活存\n0123456789014\n99\n"
    got = EsunCrawler._parse_account_overview([{"text_preview": t} for t in (spa, fx, legacy, spa)])
    assert [(a["account_no"], a["category"], a["currency"]) for a in got] == [
        ("0123456789012", "臺幣綜存", "TWD"),
        ("0123456789013", "外幣活存", "USD"),
        ("0123456789014", "臺幣活存", "TWD"),
    ]
    assert EsunCrawler._parse_account_overview([{"text_preview": "0123456789012 臺幣綜存存單"}]) == []


class _Frame:
    def __init__(self, rows):
        self.rows = rows

    def evaluate(self, _js):
        return self.rows


class _Page:
    def __init__(self, *frames):
        self.frames = list(frames)


def test_taishin_overview_table_rows_are_strict_and_deduplicated():
    row = ["未設定", "1234-56-7890123-4\n敦南Richart一類部帳戶", "年息 0.100%", "約 0元", "1,000", "-- 請選擇 --"]
    bad_balance = ["x", "1234-56-7890123-5", "r", "i", "約 9", "f"]
    two_accounts = ["1234-56-7890123-6", "1234-56-7890123-7", "a", "b", "1"]
    page = _Page(_Frame([row, bad_balance, two_accounts]), _Frame([row]))
    assert TaishinCrawler._overview_accounts(page) == [{
        "accountNo": "12345678901234", "balance": "1000",
        "accountTypeName": "敦南Richart一類部帳戶", "userdefineName": None,
    }]


class _ScbPage:
    def __init__(self, text, navigates_on="em"):
        self.text, self.navigates_on, self.clicked = text, navigates_on, []
        self.on_overview = False

    def evaluate(self, js, arg=None):
        if arg is not None:
            self.clicked.append(arg)
            self.on_overview |= arg == self.navigates_on
            return True
        return self.text if self.on_overview else "首頁"

    def wait_for_timeout(self, _ms):
        pass


def test_scb_deposit_accounts_retry_until_overview_and_parse_masked_rows():
    text = ("結構型商品查詢\n\n心幸福活期儲蓄存款 12345●●●●●6789\n\n存款總金額 TWD 1,234.50\n\n"
            "外幣活期存款 12345●●●●●6790\n存款總金額 USD 10\n")
    page = _ScbPage(text, navigates_on="a")
    got = ScbCrawler._deposit_accounts(page)
    assert page.clicked == ["em", "a"]
    assert got == [
        {"account_no": "12345●●●●●6789", "currency": "TWD", "type": "心幸福活期儲蓄存款", "balance": "1234.50"},
        {"account_no": "12345●●●●●6790", "currency": "USD", "type": "外幣活期存款", "balance": "10"},
    ]
    assert ScbCrawler._deposit_accounts(_ScbPage(text, navigates_on="none")) == []


def test_cathay_post_login_announcement_rule_is_narrow():
    from backend.banks.cathay import CathayCrawler

    rules = {r.name: r for r in CathayCrawler.login_checkpoint_rules(CathayCrawler.__new__(CathayCrawler))}
    rule = rules["cathay-post-login-announcement"]
    assert rule.action_texts == ("下次再提醒",)
    notice = "系統維護公告\n\n2026/10/05(日)01:00~06:00 配合系統維護作業，暫停證券查詢服務。\n不要再顯示\n下次再提醒"
    assert rule.required_body_pattern.search(notice)
    assert not rule.required_body_pattern.search("系統維護公告\n請輸入驗證碼\n確定")
    assert not rule.required_body_pattern.search("重要通知 服務內容提醒 我知道了")
    names = list(rules)
    assert names.index("cathay-post-login-announcement") < names.index("cathay-unknown-dialog")
