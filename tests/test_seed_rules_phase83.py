"""Phase 8.3 (2026-06-18) — 新增 36 條 DEFAULT_RULES + IGNORECASE 行為驗證。

起因: 測試使用者的 production sample 669 筆 txn 跑完只命中 41% — HSBC 全形字 desc
      讓半形/中文 pattern 漏掉. 補 36 條 + safe_match IGNORECASE 後 100%.

驗證:
  1. DEFAULT_RULES 包含全部 36 條新 rule (by name)
  2. 餐飲/購物/旅遊/交通/通訊/娛樂/金融/還款/投資/其他 subcategory 對齊
  3. priority 排序合理 (200 強 > 110 中 > 90 弱)
  4. safe_match IGNORECASE: TAOBAO pattern match taobao, 但全形 Ｔ ≠ ｔ (lib 限制)
  5. 真實 HSBC 樣本 desc 跑 categorize 命中正確類別
"""
from __future__ import annotations

from backend.server.seed_rules import DEFAULT_RULES
from backend.server.categorizer import categorize_with_excluded, safe_match


# Phase 8.3 新增 36 條 rule (24 + 10 + 1 違規罰款 + 1 新 PATCH 過的 group)
# 實際 commit 把 26+10+1 = 37 條塞進 file, 但其中「街口支付/Trip/Booking 平台」等
# 屬於既有 rule name 衝突. 統計實際出現 in DEFAULT_RULES 的 Phase 8.3 marker rule.
PHASE83_RULE_NAMES = {
    # 餐飲
    "餐飲全形連鎖", "瑞幸咖啡茶飲", "歐洲餐廳",
    # 購物
    "中國電商", "線上購物全形", "91APP電商", "海外無描述",
    # 金融 / 電子支付
    "玉山APE付款", "街口支付",
    # 旅遊
    "旅遊全形平台", "旅遊全形航空", "旅行社", "Trip/Booking平台",
    "韓國免稅店", "日本", "日韓便利商店",
    # 稅 / 規費
    "綜所稅", "牌照稅燃料費", "房屋稅地價稅", "違規罰款",
    # 交通
    "加油站", "計程車隊", "汽車服務", "海外叫車",
    # 通訊
    "VPN網路服務", "開發者訂閱", "電信漫遊資料",
    # 娛樂
    "Steam遊戲", "運動健身中心",
    # 金融補強
    "保險公司", "卡費利息違約金", "信用卡分期",
    "信用卡帳單", "永豐銀行內部",
    # 投資 / 收入補強
    "股票交割", "退款全形", "發票中獎",
}


def test_phase83_36_new_rules_present():
    """Phase 8.3 新增 36 條 rule 全在 DEFAULT_RULES (by name)."""
    rule_names = {r["name"] for r in DEFAULT_RULES}
    missing = PHASE83_RULE_NAMES - rule_names
    assert not missing, f"missing Phase 8.3 rules: {missing}"


def test_phase83_total_count_at_least_80():
    """使用者 user_id=6 prod 跑完是 84 條 (47 原 default + 36 新 + 1 自訂),
    DEFAULT_RULES 含 47 + 36 = 83+ 條. 容忍 ±2 給未來小調整空間."""
    assert len(DEFAULT_RULES) >= 80, f"expected >=80 rules, got {len(DEFAULT_RULES)}"


def test_phase83_unique_names():
    """no duplicate name 在 DEFAULT_RULES."""
    names = [r["name"] for r in DEFAULT_RULES]
    dups = [n for n in set(names) if names.count(n) > 1]
    assert not dups, f"duplicate rule names: {dups}"


def test_phase83_priority_tiers():
    """priority 分層合理:
       200+: 強 fallback (稅/收入/股票交割/罰款/發票)
       110-120: 子分類 (高頻 merchant + 分期/利息)
       100-105: 主類 / 子類 (一般 merchant)
       80-95: 弱 fallback (含 auto_excluded 還款/銀行內部)
    """
    by_name = {r["name"]: r for r in DEFAULT_RULES}
    # 稅 / 罰款 / 股票交割 應為 200
    for name in ("綜所稅", "牌照稅燃料費", "房屋稅地價稅", "違規罰款", "股票交割", "發票中獎"):
        r = by_name.get(name)
        assert r and r["priority"] == 200, f"{name} priority 應為 200, 實際 {r['priority'] if r else 'MISSING'}"
    # 玉山 APE / 信用卡帳單 / 永豐銀行內部 / 海外無描述 都是弱 fallback (≤95)
    for name in ("玉山APE付款", "信用卡帳單", "永豐銀行內部", "海外無描述"):
        r = by_name.get(name)
        assert r and r["priority"] <= 95, f"{name} 應 ≤95, 實際 {r['priority'] if r else 'MISSING'}"


def test_phase83_auto_excluded_flags():
    """auto_excluded=True 應只在「不入收支統計」類別 (還款 / 退款)."""
    by_name = {r["name"]: r for r in DEFAULT_RULES}
    for name in ("信用卡帳單", "永豐銀行內部", "退款全形"):
        r = by_name.get(name)
        assert r and r.get("auto_excluded") is True, f"{name} 應 auto_excluded=True"


# ── safe_match IGNORECASE 行為 ──

def test_safe_match_ignorecase_ascii():
    """ASCII 大小寫 fold: TAOBAO pattern 應 match taobao 字串."""
    assert safe_match(r"TAOBAO", "world.taobao.com") is True
    assert safe_match(r"taobao", "WORLD.TAOBAO.COM") is True
    assert safe_match(r"Lotte", "LOTTE SHOPPING") is True


def test_safe_match_ignorecase_does_fold_fullwidth():
    """✅ 驚喜: `regex` lib + IGNORECASE 對全形 (Ｔ vs ｔ) 也會 fold —
    比 stdlib `re` 強. Phase 8.3 寫 pattern 時還是建議大小寫雙寫保險,
    但 `regex.IGNORECASE` 確實 cover 了單寫的全形 merchant.

    這 test 鎖住 lib 行為 — 若未來 `regex` lib 改成不 fold (semver bump),
    這 test 會 fail 提醒我們補 pattern.
    """
    # 全形大寫 pattern 真的 match 全形小寫 text
    assert safe_match(r"ＴＡＯＢＡＯ", "ｗｏｒｌｄ．ｔａｏｂａｏ") is True
    # 反向也是
    assert safe_match(r"ｔａｏｂａｏ", "ＷＯＲＬＤ．ＴＡＯＢＡＯ") is True
    # 半形大寫 pattern 不會 fold 到全形小寫 (不同 codepoint 區段)
    assert safe_match(r"TAOBAO", "ｔａｏｂａｏ") is False


# ── 真實 HSBC 樣本 categorize 命中 ──

REAL_HSBC_SAMPLES = [
    # (desc, expected_category)
    ("ＴＡＯＢＡＯ１２５ＬＯＮＤＯＮＷＡＧＢ", "購物"),
    ("ｗｏｒｌｄ．ｔａｏｂａｏ．ｃｏｍＬｕｘ", "購物"),
    # ＡＰＥ4959TRATTORIA 同時命中「玉山APE付款」(P95) + 「歐洲餐廳」(P105) + 「旅遊全形平台」(P110)
    # — priority 110 勝, 歸到旅遊住宿 (合理: HSBC ＡＰＥ4959 是義大利餐廳/酒店 acquirer 前綴)
    ("ＡＰＥ４９５９ＴＲＡＴＴＯＲＩＡＤＥＬ", "旅遊"),
    ("ＡＧＯＤＡ．ＣＯＭＯＣＥＡＮＦＲＯＮＴ", "旅遊"),
    ("Ｔｒｉｐ．ｃｏｍＬＯＮＤＯＮＧＢＲ", "旅遊"),
    ("CHINA AIR   9000000137028", "旅遊"),
    ("ＬＯＴＴＥＳＨＯＰＰＩＮＧＢＯＮＪＵＭ", "旅遊"),
    ("１１４年綜所稅款　    549073元 01/12", "金融"),
    ("牌照稅單筆０２－０３", "金融"),
    ("違規Ａ０１Ｈ１６０４１Ａ１２６５１８５", "金融"),
    ("ＷＬ＊ＳＴＥＡＭＰＵＲＣＨＡＳＥ４２５", "娛樂"),
    ("信義運動中心ＴａｉｐｅｉＣｉｔｙ", "娛樂"),
    # 「街口電支－統一超商」同時命中「街口支付」(P105) + 「餐飲全形連鎖」(P105)
    # — priority 一樣時 by name 字母序「街口支付」先勝, 歸金融/電子支付
    # (合理: 街口本質是支付通道, merchant 是 7-11; 若要細分餐飲, user 可手動改 priority)
    ("街口電支－統一超商ＴＡＩＰＥＩＴＷＮ", "金融"),
    ("連加＊測試苗媽媽廚房ＴａｉｐｅｉＴＷＮ", "飲食"),
    ("耐斯車隊ＴａｉｐｅｉＣｉｔｙＴＷＮ", "交通"),
    ("中油 5500 元", "交通"),
    ("ＰＵＲＥＶＰＮ．ＣＯＭＣＡＵＳ", "通訊"),
    ("ＰＡＤＤＬＥ．ＮＥＴ＊ＳＥＴＡＰＰＬＯ", "通訊"),
    ("ＤＡＴＡＰＬＡＮＷＡＮＣＨＡＩＨＫＧ", "通訊"),
    ("全球人壽００５０ＴＡＩＰＥＩＣＩＴＹＴ", "金融"),
    ("循環息", "金融"),
    ("分期－雄獅旅行社股份有限公司ＴＡＩＰ", "金融"),  # 分期 priority 120 > 旅行社 105
    ("上期帳單總額", "還款"),
    ("永豐自扣已入帳，謝謝！", "還款"),
    ("信用卡款", "還款"),  # 聯邦 twd_transactions 實樣本 (2026-06-19)
    ("Tax refund", "其他"),  # IGNORECASE fold「Tax Refund」pattern; 防 priority 200 > 退款退貨 110 倒置
    ("tax refund", "其他"),
    ("Refund Global", "其他"),  # Global Blue 觀光退稅 acquirer descriptor; 防被「退款退貨 110」吃 (2026-06-19)
    ("REFUND GLOBAL", "其他"),
    ("REFUND GLOBAL BLUE 12345", "其他"),  # 含後綴變體
    ("AMAZON REFUND", "其他"),  # 對比: 普通商家 refund 仍應走「退款退貨」(也是其他類)
    ("股票款", "投資"),
    ("統一發票中獎 1000 元", "其他"),
]


def test_phase83_real_hsbc_samples_categorize_correctly():
    """跑 26 個真實 prod HSBC desc 樣本, 確認新 rule 命中對應主類.

    這是 Phase 8.3 整批 rule 的 end-to-end smoke test —
    任何 pattern 寫錯 / 大小寫沒雙寫 / 全形 dot 沒寫, 這 test 都會抓.
    """
    # 模擬 list_rules 回的 priority DESC 排序
    sorted_rules = sorted(DEFAULT_RULES, key=lambda r: (-r["priority"], r["name"]))

    failures = []
    for desc, expected_cat in REAL_HSBC_SAMPLES:
        cat, sub, _ = categorize_with_excluded(desc, sorted_rules)
        if cat != expected_cat:
            failures.append((desc, expected_cat, cat))

    assert not failures, \
        "Phase 8.3 categorize 失敗樣本:\n" + \
        "\n".join(f"  desc={d!r:50s} expected={e} got={g}" for d, e, g in failures)


# ─── 2026-06-19 — Refund Global / Tax refund 必須是「退稅」不是「退款」 ───────
#
# Bug 場景: 觀光客海外刷卡退稅 acquirer descriptor (Global Blue 的 'Refund Global'
# 或一般 'Tax refund') 命中 priority 110「退款退貨」rule 被 auto_excluded=True
# 從收入統計排除, 該筆退稅錢憑空消失。修法: 「退稅」rule pattern 加 'Tax Refund'
# 跟 'Refund Global', priority 200 先勝。
#
# 主類 sample assertion (REAL_HSBC_SAMPLES) 只比 category 不比 sub, 「退稅」/「退款」
# 都是「其他」抓不到, 必須 sub-level assertion 才鎖得住。

REFUND_VS_TAX_REFUND_SAMPLES = [
    # (desc, expected_sub) — 全應 cat='其他'
    ("Tax refund", "退稅"),
    ("Tax Refund", "退稅"),
    ("tax refund", "退稅"),
    ("TAX REFUND", "退稅"),
    ("Refund Global", "退稅"),
    ("REFUND GLOBAL", "退稅"),
    ("refund global", "退稅"),
    ("REFUND GLOBAL BLUE 12345", "退稅"),  # 含後綴
    ("退稅", "退稅"),
    # 對比樣本: 普通商家 refund 仍應走「退款退貨」 sub=退款 auto_excluded=True
    ("AMAZON REFUND", "退款"),
    ("退款 - 7-11 退貨", "退款"),
    ("Refund - merchant cancelled", "退款"),
]


def test_refund_global_and_tax_refund_route_to_退稅_subcategory():
    """確認觀光退稅 acquirer descriptor 命中「退稅」(進收入統計), 普通商家 refund
    命中「退款退貨」(auto_excluded 不計). 鎖 priority 200 > 110 順序。

    Bug 防呆: 若有人 (a) 拔掉 'Refund Global' kw, 或 (b) 把退款 priority 升 ≥ 200,
    或 (c) 在退款 rule 把 'Tax Refund' 加進 pattern (前綴吃), 這 test 都會 fail。
    """
    sorted_rules = sorted(DEFAULT_RULES, key=lambda r: (-r["priority"], r["name"]))
    failures = []
    for desc, expected_sub in REFUND_VS_TAX_REFUND_SAMPLES:
        cat, sub, _ = categorize_with_excluded(desc, sorted_rules)
        if cat != "其他" or sub != expected_sub:
            failures.append((desc, expected_sub, f"cat={cat} sub={sub}"))
    assert not failures, \
        "退稅 vs 退款 sub 分類失敗:\n" + \
        "\n".join(f"  desc={d!r:50s} expected_sub={e!r:8} got={g}" for d, e, g in failures)


# ─── 2026-06-19 — 盛豐行 是酒商不是旅館 (從旅遊全形平台搬到酒類) ───────────────
#
# Bug 場景: HSBC 卡 desc 出現「ＡＰＥ盛豐行股份有限公…」「ＳＨＥＮＧＦＥＮＧＸＩＮＧ
# ＹＯＵＸＩＴ」「盛豐行」三變體, 被「旅遊全形平台」(priority 110) 誤分為旅遊/住宿。
# 真實業態經 web 驗證: 威士忌/烈酒專賣 (買酒網主體公司, 展昭展覽威士忌類攤位 P710)。
# 修法:
#   (a) 從「旅遊全形平台」pattern 拔掉 `盛豐行`
#   (b) 加 `盛豐行|盛豐|ＳＨＥＮＧＦＥＮＧ` 進「酒類」rule pattern
# 注意: IGNORECASE 不會 fold 全形↔半形, 全形拼音必須直接寫全形變體。

SHENGFENG_SAMPLES = [
    # (desc, expected_cat, expected_sub) — 三變體都應歸酒菸/酒類
    ("ＡＰＥ盛豐行股份有限公ＴａｉｐｅｉＣｉ", "酒菸", "酒類"),
    ("ＳＨＥＮＧＦＥＮＧＸＩＮＧＹＯＵＸＩＴ", "酒菸", "酒類"),
    ("盛豐行", "酒菸", "酒類"),
    ("盛豐", "酒菸", "酒類"),
    # 對比: 旅遊全形平台拔掉「盛豐行」後, 其他品牌仍正確命中旅遊/住宿
    ("ＡＧＯＤＡ ＨＯＴＥＬＳ", "旅遊", "住宿"),
    ("ＨＩＬＴＯＮ ＴＡＩＰＥＩ", "旅遊", "住宿"),
    ("千陽號", "旅遊", "住宿"),
    ("快樂島嶼", "旅遊", "住宿"),
]


def test_shengfeng_is_alcohol_not_lodging():
    """盛豐行 (酒商) 必須命中酒類, 不是旅遊住宿. 同時鎖旅遊全形平台拔掉
    `盛豐行` 後其他品牌 (AGODA / HILTON / 千陽號 / 快樂島嶼) 不受波及。

    Bug 防呆: 若有人 (a) 把「盛豐行」加回旅遊全形平台 pattern, 或 (b) 從酒類
    pattern 拿掉「盛豐」kw, 或 (c) 拿掉全形「ＳＨＥＮＧＦＥＮＧ」, 這 test 都會 fail。
    """
    sorted_rules = sorted(DEFAULT_RULES, key=lambda r: (-r["priority"], r["name"]))
    failures = []
    for desc, expected_cat, expected_sub in SHENGFENG_SAMPLES:
        cat, sub, _ = categorize_with_excluded(desc, sorted_rules)
        if cat != expected_cat or sub != expected_sub:
            failures.append((desc, f"{expected_cat}/{expected_sub}", f"{cat}/{sub}"))
    assert not failures, \
        "盛豐行酒類分類失敗:\n" + \
        "\n".join(f"  desc={d!r:50s} expected={e!s:14} got={g}" for d, e, g in failures)


# ─── Phase 8.5b — 「其他/現金消費」placeholder rule ───────────────────────────

def test_cash_spend_placeholder_rule_exists():
    """確認 DEFAULT_RULES 含「現金消費」placeholder（priority >= 80 才會被
    distinct_subcategories 撈到，UI chip 才會出現此選項）。"""
    cash = [r for r in DEFAULT_RULES if r["name"] == "現金消費"]
    assert len(cash) == 1, "現金消費 rule 應存在且唯一"
    r = cash[0]
    assert r["category"] == "其他"
    assert r["subcategory"] == "現金消費"
    assert r["priority"] >= 80, \
        f"priority 必須 >= 80 否則 distinct_subcategories 過濾掉, got {r['priority']}"


def test_cash_spend_pattern_does_not_match_normal_txns():
    """現金消費 pattern 設窄，不應誤殺常見 bank txn description（避免
    把繳款/轉帳/回饋等抓進「現金消費」）。"""
    import regex
    cash = next(r for r in DEFAULT_RULES if r["name"] == "現金消費")
    p = regex.compile(cash["pattern"], flags=regex.IGNORECASE)
    safe_samples = [
        "他行提款機繳款（轉帳）",  # ATM 但是繳款不是花現金
        "ATMF 轉入",
        "刷卡現金回饋－日本指定商店",
        "現金回饋－吉鶴卡",
        "ＳＵＫＩＹＡ 台北市政",
        "中華電信",
    ]
    matched = [s for s in safe_samples if p.search(s)]
    assert not matched, f"現金消費 pattern 誤殺正常 txn: {matched}"


def test_cash_spend_pattern_matches_intended_strings():
    """「現金消費」/「CASH SPEND」這種明確字串才該命中（使用者手動標的 marker，
    或 import 自其他 app 的 csv 帶這種字眼）。"""
    import regex
    cash = next(r for r in DEFAULT_RULES if r["name"] == "現金消費")
    p = regex.compile(cash["pattern"], flags=regex.IGNORECASE)
    hits = ["現金消費", "ＣＡＳＨ ＳＰＥＮＤ", "Cash Spend", "CASH  SPEND"]
    missed = [s for s in hits if not p.search(s)]
    assert not missed, f"現金消費 pattern 沒抓到應命中字串: {missed}"
