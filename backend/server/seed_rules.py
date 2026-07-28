"""Phase 5.1 → Phase 8 → Phase 8.2 A — Default category rules seed。

新 user register 時 call seed_default_rules(uid) 自動塞一批起手規則。
Idempotent：以 (user_id, name) 為 dedup 鍵；若 name 已存在則 skip。

Phase 8.2 A (2026-06-15) — rule name 中文化:
  rule name 全改中文 (UI 顯示用). categorizer 看的是 category/subcategory,
  不看 name. income_category enum (salary/bonus/...) 是 schema 欄位值, 不改.

Phase 8 (2026-06-15) 擴充: 從 10 條 → 完整覆蓋 Phase 6 13 主類 + Phase 7 5 收入類.
使用者要求「全部可以讓使用者修改 (每個使用者新建立時我們會幫他們新增 default 的)」
+ 一鍵恢復 (POST /rules/reset).

主類分布:
  飲食 / 酒菸 / 購物 / 居住 / 交通 / 通訊 / 娛樂 / 醫療 / 教育 / 旅遊 / 金融 / 投資 / 其他
  + 訂閱 (flag, 跨主類)
  + 5 收入: 薪資 / 獎金 / 利息股息 / 投資收益 / 其他
  + 轉帳 / 還款 (flow_type='transfer')
  + 退稅 (其他收入)
  + 退款 / 回饋 (cashback, 信用卡專用, 不算 FIRE 意義)
"""
from __future__ import annotations

from backend.server import rules_repo


DEFAULT_RULES: list[dict] = [
    # ====== 13 主類 (priority 100 一般 / 80 弱 / 200 強 fallback) ======
    # 飲食
    {"name": "飲食連鎖",
     "pattern": r"麥當勞|肯德基|星巴克|路易莎|cama|7-?ELEVEN|全家|FamilyMart|頂呱呱|拉麵|餐廳|餐飲|食堂|小吃|早餐|便當|滷味|火鍋|燒肉|壽司|Uber Eats|foodpanda|熊貓",
     "category": "飲食", "subcategory": "餐廳", "priority": 100},
    {"name": "量販超市",
     "pattern": r"家樂福|全聯|大潤發|COSTCO|好市多|頂好|惠康|超市|楓康|愛買",
     "category": "飲食", "subcategory": "食品雜貨", "priority": 100},

    # 酒菸 (高頻易上癮獨立統計) — 細項在下方酒菸子分類區
    # 購物 (網購/實體)
    {"name": "線上購物",
     "pattern": r"蝦皮|PCHOME|博客來|momo|UDN|Yahoo購物|台灣樂天|誠品線上|Apple Store|Amazon|TAOBAO|淘寶|蝦皮商城|Shopee",
     "category": "購物", "subcategory": "網購", "priority": 100},
    {"name": "百貨公司",
     "pattern": r"新光三越|遠百|SOGO|微風|誠品|百貨",
     "category": "購物", "subcategory": "百貨", "priority": 100},

    # Household / personal care (2026-07-05 A 方案): 不新增「日用」主類，
    # 先把 COICOP 05 household supplies + COICOP 13 personal care 顯性化為
    # 既有主類下的子類，避免「生活用品」掉進模糊的 購物/百貨 或 其他。
    {"name": "家庭用品",
     "pattern": r"衛生紙|紙巾|廚房紙巾|餐巾紙|垃圾袋|保鮮膜|鋁箔紙|燈泡|電池|除濕盒|除濕包|收納袋|收納箱",
     "category": "購物", "subcategory": "家庭用品", "priority": 110},
    {"name": "清潔用品",
     "pattern": r"洗衣精|洗衣粉|柔軟精|清潔劑|洗碗精|漂白水|去污|除霉|除菌|消毒液|地板清潔|廁所清潔|廚房清潔",
     "category": "購物", "subcategory": "清潔用品", "priority": 110},
    {"name": "個人清潔",
     "pattern": r"牙膏|牙刷|牙線|洗髮|潤髮|沐浴|香皂|肥皂|刮鬍|衛生棉|棉條|護墊|屈臣氏(?!藥)|康是美|寶雅|POYA|Watsons|COSMED|Tomod",
     "category": "購物", "subcategory": "個人清潔", "priority": 105},
    {"name": "個人照護",
     "pattern": r"理髮|剪髮|髮廊|美髮|染髮|燙髮|護髮|髮型|沙龍|Salon|SALON|barber|Barber|BARBER|美容|美甲|修眉|睫毛|按摩|SPA|Spa|spa|皮拉提斯|除毛|保養|skincare|Skincare|SKINCARE",
     "category": "購物", "subcategory": "個人照護", "priority": 105},

    # 居住 (房租/水電/管理費/家具家電/修繕)
    {"name": "家具家電",
     "pattern": r"IKEA|宜家|HOLA|宜得利|NITORI|生活工場|家具|家電|床墊|沙發|桌椅|書櫃|衣櫃|收納櫃|燈具|窗簾",
     "category": "居住", "subcategory": "家具家電", "priority": 110},
    {"name": "居家修繕",
     "pattern": r"水電行|水電材料|五金|修繕|居家維修|房屋修繕|水管|鎖行|油漆|木工|泥作|抓漏|通馬桶|通水管",
     "category": "居住", "subcategory": "居家修繕", "priority": 110},
    {"name": "房租",
     "pattern": r"租金|房租|押金|管理費|公設費|社區",
     "category": "居住", "subcategory": "房租", "priority": 100},
    {"name": "水電瓦斯",
     "pattern": r"台水|台電|瓦斯|水費|電費|大樓",
     "category": "居住", "subcategory": "水電瓦斯", "priority": 100},

    # 交通
    {"name": "大眾運輸",
     "pattern": r"北捷|台鐵|高鐵|捷運|TPASS|悠遊卡|iPASS|台北車站|機場捷運",
     "category": "交通", "subcategory": "大眾運輸", "priority": 100},
    {"name": "計程車",
     "pattern": r"Uber|計程車|Bolt|Lyft|計程",
     "category": "交通", "subcategory": "計程車", "priority": 100},
    {"name": "自駕加油",
     "pattern": r"租車|停車|加油|中油|台塑",
     "category": "交通", "subcategory": "自駕", "priority": 100},

    # 通訊
    {"name": "手機電信",
     "pattern": r"中華電信|台灣大哥大|遠傳|亞太電信|Hinet|MOD|寬頻",
     "category": "通訊", "subcategory": "電信費", "priority": 105},
    {"name": "電信寬頻",
     "pattern": r"中華電信|台灣大哥大|遠傳|亞太電信|Hinet|MOD|寬頻",
     "category": "通訊", "priority": 100},

    # 娛樂
    {"name": "電影院",
     "pattern": r"電影|威秀|秀泰|國賓|MUVIE|IMAX",
     "category": "娛樂", "subcategory": "電影", "priority": 105},
    {"name": "遊戲娛樂",
     "pattern": r"遊戲|Steam|PlayStation|Nintendo|Xbox|Roblox|Riot",
     "category": "娛樂", "subcategory": "遊戲", "priority": 105},
    {"name": "健身運動",
     "pattern": r"健身|World Gym|Being fit|瑜珈|Yoga|跑步|Curves",
     "category": "娛樂", "subcategory": "健身", "priority": 105},
    {"name": "娛樂綜合",
     "pattern": r"電影|威秀|秀泰|國賓|KTV|錢櫃|好樂迪|遊戲|Steam|PlayStation|Nintendo|Xbox|健身|World Gym|Being fit|演唱會|展覽|球賽",
     "category": "娛樂", "priority": 100},

    # 醫療
    {"name": "藥局",
     "pattern": r"藥局|藥房|大樹|杏一|丁丁|健康人生|屈臣氏藥",
     "category": "醫療", "subcategory": "藥局", "priority": 105},
    {"name": "牙醫",
     "pattern": r"牙醫|牙科|齒科",
     "category": "醫療", "subcategory": "牙醫", "priority": 105},
    {"name": "診所",
     "pattern": r"診所|皮膚科|耳鼻喉|眼科|中醫|復健",
     "category": "醫療", "subcategory": "診所", "priority": 105},
    {"name": "醫院",
     "pattern": r"醫院|長庚|台大|榮總|馬偕|新光醫|國泰醫",
     "category": "醫療", "subcategory": "醫院", "priority": 105},
    {"name": "醫療綜合",
     "pattern": r"醫院|診所|藥局|藥房|大樹|健保|牙醫|眼科|皮膚科|耳鼻喉|中醫|復健|體檢",
     "category": "醫療", "priority": 100},

    # 教育
    {"name": "書籍",
     "pattern": r"金石堂|誠品書店|博客來書|書局|書店",
     "category": "教育", "subcategory": "書籍", "priority": 105},
    {"name": "線上課程",
     "pattern": r"udemy|Coursera|Udacity|edX|線上課|課程|MOOC",
     "category": "教育", "subcategory": "課程", "priority": 105},
    {"name": "學費補習",
     "pattern": r"學費|補習|大學|學院|高中|國中|國小|幼兒園",
     "category": "教育", "subcategory": "學費", "priority": 105},
    {"name": "教育綜合",
     "pattern": r"學費|補習|書局|金石堂|誠品書店|博客來書|課程|教育|大學|學院|udemy|Coursera|線上課",
     "category": "教育", "priority": 100},

    # 旅遊
    {"name": "機票航空",
     "pattern": r"華航|長榮|星宇|台灣虎航|國泰航|日航|全日空|新航|機票|航空|airlines|Airlines",
     "category": "旅遊", "subcategory": "機票", "priority": 105},
    {"name": "住宿訂房",
     "pattern": r"booking|Booking\.com|Agoda|Hotels\.com|Airbnb|旅館|飯店|民宿|hostel|Hostel|H會館|Hilton|Marriott|Hyatt",
     "category": "旅遊", "subcategory": "住宿", "priority": 105},
    {"name": "旅行社行程",
     "pattern": r"可樂旅遊|易遊網|雄獅旅遊|燦星|kkday|KKday|Klook",
     "category": "旅遊", "subcategory": "行程", "priority": 105},
    {"name": "旅遊綜合",
     "pattern": r"華航|長榮|星宇|台灣虎航|可樂旅遊|易遊網|booking|Booking\.com|Agoda|Hotels\.com|Airbnb|機票|旅館|飯店|民宿|機場",
     "category": "旅遊", "priority": 100},

    # 酒菸
    {"name": "酒類",
     # 2026-06-19: 加 `盛豐行|盛豐|ＳＨＥＮＧＦＥＮＧ` — HSBC 卡 desc 三變體
     #   (a) 'ＡＰＥ盛豐行股份有限公…' (全形混入中文)
     #   (b) 'ＳＨＥＮＧＦＥＮＧＸＩＮＧＹＯＵＸＩＴ' (全形拼音 SHENGFENG XING YOU XI[AN] T[AIPEI] 截斷)
     #   (c) '盛豐行' 半形中文 (其他 acquirer)
     # IGNORECASE 不會 fold 全形↔半形, 必須直接寫全形變體; 但同字寬度大小寫會 fold。
     # 原本被「旅遊全形平台」誤吃 (priority 110), 修法是同步從那條 pattern 拔掉 `盛豐行`。
     # 真實業態: 威士忌/烈酒專賣 (「買酒網」品牌主體公司, 展昭威士忌類攤位 P710)。
     "pattern": r"啤酒|whisky|whiskey|wine|liquor|sake|清酒|高粱|紅酒|白酒|烈酒|酒商|盛豐行|盛豐|ＳＨＥＮＧＦＥＮＧ",
     "category": "酒菸", "subcategory": "酒類", "priority": 105},
    {"name": "菸類",
     "pattern": r"香菸|電子菸|菸|tobacco|cigar|Tobacco",
     "category": "酒菸", "subcategory": "菸類", "priority": 105},
    {"name": "菸酒綜合",
     "pattern": r"煙酒|菸酒|酒商|啤酒|whisky|whiskey|wine|liquor|tobacco|香菸|電子菸",
     "category": "酒菸", "priority": 100},

    # 投資
    {"name": "股票證券",
     "pattern": r"證券交割|股款|證券|永豐金證|元大證|凱基證|富邦證|玉山證|台新證|國泰證",
     "category": "投資", "subcategory": "股票", "priority": 105},
    {"name": "ETF",
     "pattern": r"ETF|0050|0056|00878|00919|VTI|VOO|SPY|QQQ",
     "category": "投資", "subcategory": "ETF", "priority": 105},
    {"name": "基金定期定額",
     "pattern": r"基金|定期定額|Vanguard|Schwab|Fidelity",
     "category": "投資", "subcategory": "基金", "priority": 105},
    {"name": "投資綜合",
     "pattern": r"證券交割|股款|證券|永豐金證|元大證|凱基證|富邦證|玉山證|台新證|ETF|基金|定期定額",
     "category": "投資", "priority": 100},

    # 金融 (含 fee, 跟 flow_type=expense 配)
    # 2026-07-04 拆分 (使用者指示 Layer 2): 「手續費」原 pattern 包了「年費|管理費|手續費|Fee」
    # 一鍋，導致「金融/手續費」stats 混了信用卡年費 + 手續費 + 管理費. 拆成 3 條:
    #   - 信用卡年費 (priority 95, 早於手續費 80): 專吃「年費/Annual Fee/MEMBER FEE」
    #   - 年費減免/退回 (priority 100, 早於年費 95): 專吃「年費減免/沖銷/退回」
    #   - 手續費 (priority 80, 原本 pattern 拿掉「年費」): 只剩真手續費/管理費/跨行費/匯費
    # 效果: 金融 主類下能看到「年費 vs 手續費 vs 保險」的乾淨 subcategory 分佈.
    #
    # ⚠️ 為什麼「信用卡年費減免」NO auto_excluded (2026-07-04 使用者抓包):
    #   已經有 Layer 1 (txn_type=fee_waiver → cashflow_direction=income) 讓前端統計正確.
    #   Layer 2 若再 auto_excluded=True → frontend computePeriodStats skip 該 row →
    #   月統計收入卡片憑空少 5000. 這是跟 2026-06-22 HSBC 退稅 bug 同一 class 的
    #   double-classification 問題. 「回饋/退款」rule 用 auto_excluded 是因為它們
    #   backend 的 flow_type 不是 income, 需要 Layer 2 補; fee_waiver 兩層都是 income
    #   一致, Layer 2 不能 exclude.
    #   詳見 wiki [[frontend-cross-layer-display-vs-stats-consistency]].
    {"name": "信用卡年費減免",
     "pattern": r"年費減免|年費沖銷|年費退回|年費退款|手續費減免|手續費退回|手續費沖銷|利息減免|利息退回|利息沖銷",
     "category": "金融", "subcategory": "年費減免", "priority": 100},
    {"name": "信用卡年費",
     "pattern": r"年費|Annual Fee|ANNUAL MEMBERSHIP|Annual Membership|ANNUAL MEMBER|Annual Member|MEMBER FEE|Member Fee",
     "category": "金融", "subcategory": "年費", "priority": 95},
    # 2026-07-28: 「國外交易手續費」必須高於所有商家 rule。
    # recategorize migration dry-run 抓到: HSBC 的手續費 row description 是
    # 「國外交易手續費ＡＬＰ＊Ｔａｏｂａｏ」(手續費 + 原始商家名黏在一起),
    # 被 priority 110「中國電商」搶走判成『購物/網購』, 但那筆 NT$17 是手續費不是
    # 淘寶消費。同型還有「國外交易手續費ＴＲＥＮＩＴＡＬＩＡ」→ 誤判旅遊/機票、
    # 「國外交易手續費ＨＯＴＥＬＧＲＥＥＮＰＬ」→ 誤判旅遊/住宿。
    # priority 300 高於所有商家 rule (最高 110) 與收入類 (最高 250)。
    {"name": "國外交易手續費",
     "pattern": r"國外交易手續費|海外交易手續費|國際交易手續費|Foreign Transaction Fee|FOREIGN TXN FEE",
     "category": "金融", "subcategory": "手續費", "priority": 300},
    {"name": "手續費",
     "pattern": r"手續費|管理費|Fee|跨行費|匯費",
     "category": "金融", "subcategory": "手續費", "priority": 80},
    {"name": "保險",
     "pattern": r"國泰人壽|富邦人壽|新光人壽|南山人壽|中國人壽|保險|保費|investlink",
     "category": "金融", "subcategory": "保險", "priority": 100},

    # 投資 (flow_type='investment' 用, 對 dashboard 不算消費) — 細項已在上方投資子分類區
    # ====== 跨類 flag ======
    # 訂閱 (跨多主類, frontend 用 is_subscription flag 高亮)
    {"name": "訂閱服務",
     "pattern": r"Netflix|Spotify|YouTube|iCloud|ChatGPT|Apple\.com/Bill|Google|Disney\+|Disney Plus|HBO|MyVideo|KKBOX|GitHub Copilot|GitHub|Adobe|Notion|Figma|Office 365",
     "category": "娛樂", "subcategory": "訂閱", "priority": 110},

    # ====== Flow_type='transfer' / 'income' 5 類 ======
    # 轉帳 / 還款 — auto_excluded=True (by definition 不算收支)
    # 2026-06-15: 拔掉「台幣匯款」KW — 該字串是永豐「跨行匯入」官方交易類別名,
    # 涵蓋「跨行轉入薪資/朋友轉錢/退款」等 — 不該預設當 transfer 排除。改靠
    # 「轉聯邦」「轉入帳戶」等具體去向字眼判斷。Microsoft 薪資 row description=
    # 「台幣匯款」一筆被誤吃 → 不入 income stats, 修法見 wiki Phase 8.4 lesson。
    {"name": "轉帳匯款",
     "pattern": r"轉帳|匯款|Transfer|ATM|跨行|ATMF",
     "category": "轉帳", "priority": 90, "auto_excluded": True},
    {"name": "信用卡還款",
     "pattern": r"自動扣繳|本行扣繳|信用卡費|信用卡款|Payment|自動扣款|全國繳費網繳款|網路銀行繳款|提款機繳款|繳費網繳款",
     "category": "還款", "priority": 90, "auto_excluded": True},

    # 5 收入類 (priority 200 強, 避開 KW 誤判進消費)
    # 2026-06-15: 薪資 pattern 擴 (Microsoft|Payroll|...) — 永豐跨行薪資 row
    # description=「台幣匯款」 + counterparty_acct=「MICROSOFT TAIWAN CORPORATION...」
    # → rule 對 counterparty 也命中. 命中 priority 250 比轉帳匯款 90 高, 不會被吃。
    {"name": "薪資",
     "pattern": r"薪資|薪轉|薪津|工資|月薪|Salary|SALARY|Payroll|PAYROLL|"
                r"MICROSOFT|Microsoft|Apple Inc|GOOGLE|Google|"
                r"台積|TSMC|聯發科|MEDIATEK|鴻海|FOXCONN",
     "category": "薪資", "priority": 250},
    {"name": "獎金",
     "pattern": r"獎金|三節|年終|業績|推薦獎金|績效獎金|Bonus|BONUS",
     "category": "獎金", "priority": 200},
    {"name": "利息股息",
     "pattern": r"利息|股息|債息|配息|股利|Interest|INTEREST|Dividend|DIVIDEND",
     "category": "利息股息", "priority": 200},
    {"name": "投資收益",
     "pattern": r"證券交割款|資本利得|租金收入|Rental|Capital Gain|CAPITAL GAIN|現金股利",
     "category": "投資收益", "priority": 200},
    # 「退稅」rule pattern 收的兩類來源：
    #   (a) 中文官方字面: 退稅 / 綜所稅退 / 稅款退 — 國稅局/政府退款
    #   (b) 英文 acquirer descriptor: Tax Refund / Refund Global — 觀光客海外刷卡退稅
    #       (Refund Global = Global Blue 退稅服務, 卡 desc 多為「REFUND GLOBAL」)
    # priority 200 必須勝過 priority 110「退款退貨」, 否則 'Refund Global' 會命中
    # 後者被誤標 auto_excluded 從收支統計消失。
    # 大小寫: 不必寫多種變體, safe_match 預設 regex.IGNORECASE flag (Phase 8.3) 自動 fold.
    {"name": "退稅",
     "pattern": r"退稅|綜所稅退|稅款退|Tax Refund|Refund Global",
     "category": "其他", "subcategory": "退稅", "priority": 200},

    # 信用卡 refund / cashback (income_category=None, 不算 FIRE)
    # auto_excluded=True — 回饋/退款不算「收入」也不算「支出」, 對使用者是 deduct 卡費
    {"name": "刷卡回饋",
     "pattern": r"刷卡現金回饋|現金回饋|JCB_CB|CB_ARIGATO|刷卡金",
     "category": "其他", "subcategory": "回饋", "priority": 110, "auto_excluded": True},
    {"name": "退款退貨",
     "pattern": r"退款|refund|Refund|REFUND|退貨",
     "category": "其他", "subcategory": "退款", "priority": 110, "auto_excluded": True},
    # 現金消費 — placeholder rule，讓「其他/現金消費」這個 subcat 在 UI chip
    # 出現（rules_repo.distinct_subcategories min_priority=80 預設過濾）。
    # pattern 設窄到幾乎不會誤殺真實 bank txn description；主要靠使用者在 App
    # 把領出來實際花掉的現金手動分類成此項，Phase 8.4 force=False 後不被覆寫。
    {"name": "現金消費",
     "pattern": r"現金消費|ＣＡＳＨ\s*ＳＰＥＮＤ|CASH\s+SPEND",
     "category": "其他", "subcategory": "現金消費", "priority": 80},

    # ============================================================================
    # Phase 8.3 (2026-06-18) — 全形 merchant + long-tail 補洞
    #
    # 起因: 測試使用者的 production sample 669 筆 txn 跑完只命中 41% (379 筆未分類).
    # 主因 HSBC 信用卡 desc 全形字 (ＡＰＥxxx / ＴＡＯＢＡＯ / 街口電支－),
    # 半形/中文 default pattern 漏掉. 補 37 條後命中率 100%.
    #
    # ⚠️ 鐵則: backend/server/categorizer.py safe_match() 用 regex.IGNORECASE
    #         (2026-06-18 fix), 但全形大小寫 (Ｔ vs ｔ, Ｗ vs ｗ) 仍不會 fold.
    #         Pattern 必須大小寫雙寫. 詳見 wiki [[fullwidth-regex-case-folding-pitfall]].
    # ============================================================================

    # ─── 餐飲全形連鎖 / 連加*/ 街口電支－/ SUKIYA / 義式餐廳 ───
    {"name": "餐飲全形連鎖",
     "pattern": r"ＳＵＫＩＹＡ|SUKIYA|連加|街口電支|可不可熟成|瑞苗媽媽|春陽茶事|創義麵|義麵|"
                r"拉麵店|麵屋|燒肉|壽司郎|ＴａｐＰａｙ|TapPay|ＡＰＥ.*美食|１０１美食|"
                r"美食街|餐酒|食堂|お好み|定食|cafe|CAFE|Cafe",
     "category": "飲食", "subcategory": "餐廳", "priority": 105},
    {"name": "瑞幸咖啡茶飲",
     "pattern": r"瑞幸|luckin|LUCKIN|手搖|清心|大苑子|五十嵐|龜記|迷客夏|麻古|kebuke|可不可",
     "category": "飲食", "subcategory": "餐廳", "priority": 105},
    {"name": "歐洲餐廳",
     "pattern": r"ＴＲＡＴＴＯＲＩＡ|ＯＳＴＥＲＩＡ|ＶＡＤＡＮ|ＣＯＮＡＤ|Ｓ\.Ｇｉａｃｏｍｏ|"
                r"ＧＥＬＡＴＥＲＩＡ|Ｍａｋｅ－Ｉｎ－Ｓｉｌａ|ＬＡＲＩＮＡＳＣＥＮＴＥ|"
                r"ＦＯＯＤＡＴＥＬＩＥＲ|ＢＯＵＳＱＵＥＴ|ＤＪＶＥＲＳＲＬ|ＥＴＲＵＲＩＡ|"
                r"ＶＩＮＣＥＮＺＯＣＡＰＵ|Ｄｒａｆ",
     "category": "飲食", "subcategory": "餐廳", "priority": 105},

    # ─── 中國電商 全形大小寫雙寫 + 半形/全形 dot ───
    {"name": "中國電商",
     "pattern": r"ＴＡＯＢＡＯ|ｔａｏｂａｏ|淘寶|淘宝|ＡＬＰ＊Ｔａｏｂ|ALP\*Taob|"
                r"ｗｏｒｌｄ\.ｔａｏｂａｏ|ｗｏｒｌｄ．ｔａｏｂａｏ|world\.taobao|"
                r"Ａｌｉｐａｙ|Alipay|ＡＬＩＰＡＹ|ＴＭＡＬＬ|ｔｍａｌｌ|TMALL|"
                r"天猫|京東|JD\.com|拼多多|微信支付|WeChat Pay|ＷＥＣＨＡＴ|ｗｅｃｈａｔ",
     "category": "購物", "subcategory": "網購", "priority": 110},
    {"name": "線上購物全形",
     "pattern": r"ＰＣＨＯＭＥ|ＰＣ ＨＯＭＥ|ＳＨＯＰＥＥ|ｓｈｏｐｅｅ|ＭＯＭＯ|ｍｏｍｏ|"
                r"博客來|誠品線上|Yahoo購物|ＡＰＰＬＥ\.ＣＯＭ|Ａｐｐｌｅ\.ｃｏｍ|"
                r"蘋果電腦|Apple Store|思琳|迪卡儂|ＤＥＣＡＴＨＬＯ|UNIQLO|ＵＮＩＱＬＯ",
     "category": "購物", "subcategory": "網購", "priority": 105},
    {"name": "91APP電商",
     "pattern": r"９１ＡＰＰ|91APP|91Ａｐｐ",
     "category": "購物", "subcategory": "網購", "priority": 105},
    {"name": "海外無描述",
     "pattern": r"暫無資訊|無卡延提|ＡＣＨ代付|ACH代付|ＰＡＹＳＥＣＵＲＥ|２Ｃ２Ｐ|2C2P|"
                r"ＰａｘｃｌｏｕｄＬｉｍｉｔｅｄ|Paxcloud|ＧｏＰｏｃｋｅｔ",
     "category": "購物", "subcategory": "海外其他", "priority": 90},

    # ─── 玉山 APE pay-with-line 系列 (HSBC 大量未命中前綴) ───
    {"name": "玉山APE付款",
     "pattern": r"^ＡＰＥ|^APE\d|ＡＰＥ４９５９|ＡＰＥ４７２２",
     "category": "金融", "subcategory": "電子支付", "priority": 95},
    {"name": "街口支付",
     "pattern": r"街口|ＴＷＱＲ|TWQR|跨機構購物|電子支付",
     "category": "金融", "subcategory": "電子支付", "priority": 105},

    # ─── 旅遊 全形平台 / 全形航空 / 旅行社 ───
    # 注意: 2026-06-19 拔掉 `盛豐行` — 該店是威士忌/烈酒專賣 (買酒網品牌主體),
    # 移至「酒類」rule. 原本因「盛豐」字面像旅館品牌 (千陽號/快樂島嶼同檔次幻想)
    # 被誤分這條, 純屬命名巧合 — 真實 acquirer descriptor `ＡＰＥ盛豐行股份有限公…`
    # 是 HSBC 卡實樣本, 業態經 web 驗證確實是酒商 (展昭威士忌類攤位 P710)。
    {"name": "旅遊全形平台",
     "pattern": r"ＡＧＯＤＡ|Ａｇｏｄａ|ＢＯＯＫＩＮＧ|Ｂｏｏｋｉｎｇ|ＨＯＴＥＬＳ\.ＣＯＭ|"
                r"ＡＩＲＢＮＢ|Ａｉｒｂｎｂ|ＨＩＬＴＯＮ|Ｈｉｌｔｏｎ|ＭＡＲＲＩＯＴＴ|Ｍａｒｒｉｏｔｔ|"
                r"ＨＹＡＴＴ|Ｈｙａｔｔ|千陽號|城市車旅|國泰置地|快樂島嶼|"
                r"Ｊｏｙ Ｆｉｒｅ|ＨＯＴＥＬ|ＨＴＬ|ＴＲＡＴＴＯＲＩＡ|ＯＳＴＥＲＩＡ|"
                r"ＶＡＤＡＮ|ＰＲＡＤＡ.*ＬＥＣＣＩＯ",
     "category": "旅遊", "subcategory": "住宿", "priority": 110},
    {"name": "旅遊全形航空",
     "pattern": r"ＣＨＩＮＡ ＡＩＲ|CHINA AIR|CHINA[ ]+AIR|ＣＡＴＨＡＹ ＰＡＣ|"
                r"ＥＶＡ ＡＩＲ|ＥＶＡＡＩＲ|ＳＴＡＲＬＵＸ|ＦＬＹＳＣＯＯＴ|Ｓｃｏｏｔ|"
                r"ＡＮＡ|ＪＡＬ|ＴＲＥＮＩＴＡＬＩＡ|ＡＵＴＯＳＴＲＡＤＥ|高速公路|"
                r"ＭＶＣＩＡＳＩＡ|ＭＶＣＩ|Ｍａｒｒｉｏｔｔ Ｖａｃａ",
     "category": "旅遊", "subcategory": "機票", "priority": 110},
    {"name": "旅行社",
     "pattern": r"雄獅旅行社|可樂旅遊|易遊網|燦星|ｋｋｄａｙ|ＫＫＤＡＹ|ＫＬＯＯＫ|Ｋｌｏｏｋ|分期－雄獅",
     "category": "旅遊", "subcategory": "行程", "priority": 105},
    {"name": "Trip/Booking平台",
     "pattern": r"Ｔｒｉｐ\.ｃｏｍ|Ｔｒｉｐ．ｃｏｍ|ＴＲＩＰ\.ＣＯＭ|ＴＲＩＰ．ＣＯＭ|Trip\.com|"
                r"ＷＷＷ\.ＭＩＤＡＴＩＣＫＥＴ|ＷＷＷ．ＭＩＤＡＴＩＣＫＥＴ|MIDATICKET|Mida ?Ticket|"
                r"ＰｌａｎｅｔＴａｘＦｒｅｅ|TaxFree|ＰＯＰＭＡＲＴ|POPMART",
     "category": "旅遊", "subcategory": "其他", "priority": 110},
    {"name": "韓國免稅店",
     "pattern": r"ＬＯＴＴＥ ＤＦＳ|ＬＯＴＴＥＤＦＳ|ＬＯＴＴＥ ＳＨＯＰ|ＬＯＴＴＥＳＨＯＰ|ＬＯＴＴＥ|"
                r"ｌｏｔｔｅ|ＳＨＩＮＳＥＧＡＥ|ｓｈｉｎｓｅｇａｅ|ＳＥＯＵＬ|ｓｅｏｕｌ|韓國|"
                r"ＰＯＳＩＪＥＵＮＡＵＬＬ|Ｊｅｊｕ|ＪＥＪＵ|ＢＢＥＵＭ|ＲＥＡＭＥＲＥＡＤ|"
                r"ＴＯＯＢＢＯＯＬ|ＪＥＮＵＬＵＬ|ＢＯＮＪＵＭ|ＫＲＰＬｏｔｔｅ",
     "category": "旅遊", "subcategory": "其他", "priority": 105},
    {"name": "日本",
     "pattern": r"ＦＡＭＩＬＹ ＭＡＲＴ.*ＪＰ|ＴＯＫＹＯ|ＯＳＡＫＡ|ＫＹＯＴＯ|ＮＡＲＩＴＡ|"
                r"ＨＡＮＥＤＡ|ＳＵＩＣＡ",
     "category": "旅遊", "subcategory": "其他", "priority": 105},
    {"name": "日韓便利商店",
     "pattern": r"ＭＡＸＶＡＬＵ|MaxValu|ＴＯＮＧＫＥＵＮ|ＩＮＬＩＮＥ|ＳＨＩＮＳＥＧＡＥ|"
                r"ＧＴＦＫｏｒｅａ|GTF",
     "category": "旅遊", "subcategory": "餐飲", "priority": 105},

    # ─── 稅 / 規費 (priority 200 強, 蓋過所有消費) ───
    {"name": "綜所稅",
     "pattern": r"綜所稅款|綜所稅|綜合所得稅|個人所得稅",
     "category": "金融", "subcategory": "稅", "priority": 200},
    {"name": "牌照稅燃料費",
     "pattern": r"牌照稅|燃料費|燃料稅|汽燃費|路用稅",
     "category": "金融", "subcategory": "稅", "priority": 200},
    {"name": "房屋稅地價稅",
     "pattern": r"房屋稅|地價稅|契稅|印花稅|遺贈稅",
     "category": "金融", "subcategory": "稅", "priority": 200},
    {"name": "違規罰款",
     "pattern": r"違規|罰單|罰款|交通違規|超速|違停|違規Ａ|違規A",
     "category": "金融", "subcategory": "罰款", "priority": 200},

    # ─── 加油 / 計程車 / 汽車 / 海外叫車 ───
    {"name": "加油站",
     "pattern": r"中油|台塑|台亞|台糖加油|Ｐｅｔｒｏ|ＰＥＴＲＯ|ＣＰＣ|"
                r"Ａｕｔｏｐａｓｓ|ＡＵＴＯＰＡＳＳ|加油|油站",
     "category": "交通", "subcategory": "自駕", "priority": 110},
    {"name": "計程車隊",
     "pattern": r"耐斯車隊|幸福車隊|台灣大車隊|大都會車隊|博歐特|55688|550688|"
                r"綠界－全鋒|貴賓專接|全鋒貴賓",
     "category": "交通", "subcategory": "計程車", "priority": 105},
    {"name": "汽車服務",
     "pattern": r"弘緯汽車|全鋒汽車|汽車有限公司|修車|保養廠|租車公司|和運|格上|"
                r"ｉＲｅｎｔ|ＩＲＥＮＴ",
     "category": "交通", "subcategory": "汽車", "priority": 110},
    {"name": "海外叫車",
     "pattern": r"ＢＯＬＴ|Ｂｏｌｔ|^Bolt|ＧＲＡＢ|Ｇｒａｂ|ＷＷＷ\.ＧＲＡＢ|"
                r"ＤｉＤｉ|ＤＩＤＩ|Lyft",
     "category": "交通", "subcategory": "計程車", "priority": 110},

    # ─── 通訊 / 訂閱 ───
    {"name": "VPN網路服務",
     "pattern": r"ＰＵＲＥＶＰＮ|PureVPN|ＮＯＲＤＶＰＮ|NordVPN|ＥＸＰＲＥＳＳＶＰＮ|"
                r"ExpressVPN|ProtonVPN|ｉａｎｙｇｏ|ＩＡＮＹＧＯ|ianyGo|ＯＰＹ",
     "category": "通訊", "subcategory": "網路服務", "priority": 105},
    {"name": "開發者訂閱",
     "pattern": r"ＰＡＤＤＬＥ|Paddle|ＳＥＴＡＰＰ|SetApp|ＮＡＭＥ－ＣＨＥＡＰ|NameCheap|"
                r"ＤＯＭＡＩＮ|Cloudflare|Vercel|Netlify",
     "category": "通訊", "subcategory": "SaaS", "priority": 110},
    {"name": "電信漫遊資料",
     "pattern": r"DATA PLAN|DATAPLAN|ＤＡＴＡ ＰＬＡＮ|ＤＡＴＡＰＬＡＮ|數據漫遊|漫遊上網|"
                r"ＲＯＡＭＩＮＧ|Ｒｏａｍｉｎｇ|暫無資訊.*ＵＳＤ|暫無資訊.*SGD|"
                r"暫無資訊.*USD|暫無資訊.*ＳＧＤ|ＷＡＮＣＨＡＩＨＫＧ|ＨＫＧ",
     "category": "通訊", "subcategory": "漫遊", "priority": 110},

    # ─── 娛樂 ───
    {"name": "Steam遊戲",
     "pattern": r"ＳＴＥＡＭ|Ｓｔｅａｍ|STEAMPURCHASE|ＳＴＥＡＭＰＵＲＣＨＡＳＥ|ＷＬ＊ＳＴＥＡＭ",
     "category": "娛樂", "subcategory": "遊戲", "priority": 110},
    {"name": "運動健身中心",
     "pattern": r"信義運動中心|大安運動中心|運動中心|Ｗｏｒｌｄ Ｇｙｍ|ＷＯＲＬＤ ＧＹＭ|"
                r"健身工廠|Anytime Fitness",
     "category": "娛樂", "subcategory": "健身", "priority": 110},

    # ─── 金融 / 還款補強 ───
    {"name": "保險公司",
     "pattern": r"全球人壽|國泰人壽|富邦人壽|新光人壽|南山人壽|中國人壽|台灣人壽|"
                r"三商美邦|宏泰人壽|明台產險|富邦產險|新光產險|國泰世紀產險|和泰產險",
     "category": "金融", "subcategory": "保險", "priority": 110},
    {"name": "卡費利息違約金",
     "pattern": r"循環息|循環利息|違約金|減少違約金|滯納金|遲繳|預借現金|預借手續費",
     "category": "金融", "subcategory": "手續費", "priority": 120},
    {"name": "信用卡分期",
     "pattern": r"^分期|信用卡分期|每期攤付|分期－",
     "category": "金融", "subcategory": "分期", "priority": 120},
    {"name": "信用卡帳單", "auto_excluded": True,
     "pattern": r"上期帳單|上期應繳|本期應繳|帳單總額|本期帳單|信用卡帳單",
     "category": "還款", "subcategory": "信用卡", "priority": 95},
    {"name": "永豐銀行內部", "auto_excluded": True,
     "pattern": r"永豐自扣|大戶 PL|大戶PL|大戶帳戶|消費回饋入帳戶|數位帳戶|薪轉帳戶|網銀預約",
     "category": "還款", "subcategory": "銀行內部", "priority": 90},

    # ─── 投資 / 收入補強 ───
    {"name": "股票交割",
     "pattern": r"股票款|股款|證券交割|ＡＴＭＦ 延轉.*證|延轉.*證券",
     "category": "投資", "subcategory": "股票", "priority": 200},
    {"name": "退款全形", "auto_excluded": True,
     "pattern": r"Ｒｅｆｕｎｄ|ＲＥＦＵＮＤ|退款|退貨|退費|Ｇｌｏｂａｌｂｌｕｅ|ＧＬＯＢＡＬＢＬＵＥ",
     "category": "其他", "subcategory": "退款", "priority": 115},
    {"name": "發票中獎",
     "pattern": r"統一發票|統一獎|電子發票|發票中獎",
     "category": "其他", "subcategory": "發票", "priority": 200},
]


def seed_default_rules(user_id: int) -> int:
    """為新 user 塞 DEFAULT_RULES。已存在同 name 的 rule 會 skip（idempotent）。
    回傳實際新增的條數。
    """
    existing_names = {r["name"] for r in rules_repo.list_rules(user_id=user_id)}
    added = 0
    for r in DEFAULT_RULES:
        if r["name"] in existing_names:
            continue
        rules_repo.create_rule(
            user_id=user_id,
            name=r["name"],
            pattern=r["pattern"],
            category=r["category"],
            subcategory=r.get("subcategory"),
            priority=r["priority"],
            enabled=True,
            auto_excluded=bool(r.get("auto_excluded", False)),
        )
        added += 1
    return added


def reset_to_defaults(user_id: int) -> dict:
    """Phase 8 (2026-06-15 使用者指示): 一鍵恢復預設.

    砍掉 user 所有 rule 重塞 DEFAULT_RULES (給手滑救援).
    回傳 {deleted: int, added: int} 供 frontend 顯示成果.
    """
    # 先 list 全部, 砍掉
    existing = rules_repo.list_rules(user_id=user_id)
    deleted = 0
    for r in existing:
        if rules_repo.delete_rule(user_id=user_id, rule_id=r["id"]):
            deleted += 1
    # 再塞 default
    added = seed_default_rules(user_id=user_id)
    return {"deleted": deleted, "added": added}
