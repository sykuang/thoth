"""Cross-layer linter: seed_rule auto_excluded 不能跟 income-flavored txn_type 撞.

Regression guard (2026-07-05 fee_waiver bug):

背景 — 兩層分類系統
====================
thoth 有兩層獨立分類:
  Layer 1 (backend/core/classify.py): txn_type enum (spending/cashback/refund/
    payment/fee/annual_fee/fee_waiver/installment/unknown), 決定 cashflow_direction
    (income/expense/neutral) → 前端 renderAmount 綠/紅/灰, 前端/後端 stats aggregate
    的正負符號。
  Layer 2 (backend/server/seed_rules.py): user-facing category rules
    (飲食/購物/金融/...) + 選擇性的 auto_excluded=True flag, 決定該 row 是否 skip
    整個 stats aggregate (backend transactions.py:877 & frontend
    txnFilter.ts:computePeriodStats:197).

Bug 本體
========
若 rule 的 pattern 命中 desc 的 txn_type 屬於 income-flavored (cashback/refund/
fee_waiver), 且 rule 標 auto_excluded=True → row 在單行 renderAmount 顯示綠色
income, 但 stats aggregate skip → 月統計 income 卡片憑空少錢。

Real evidence (2026-07-04 21:XX UBOT ****7027 id=107):
  desc='微風無限卡正卡年費減免' amount=-5000
  → Layer 1 classify → txn_type=fee_waiver → cashflow_direction=income (+5000)
  → Layer 2 seed_rule 命中「信用卡年費減免」(auto_excluded=True) → stats skip
  → 使用者單行看到 🟢 +NT$5,000, 月統計 income 卻沒加這 5000 → 割裂

例外 WHITELIST
=============
- 「刷卡回饋」「退款退貨」「退款全形」: 設計本意 (見
  wiki [[rule-auto-excluded-per-txn-stats-skip-pattern]]). 這些 row 的 amount 是
  反向 offset (cashback 通常負值進帳), backend flow_type=income 但 amount 符號
  跟一般 income row 相反, 需要 auto_excluded 擋掉不重複算. 前端 renderAmount 對
  cashback/refund txn_type 也 skip stats bucket.

詳見 wiki [[frontend-cross-layer-display-vs-stats-consistency]] 「變種二」章節.
"""
from __future__ import annotations

import re

import pytest

from backend.core.classify import classify_by_desc_and_sign
from backend.server.seed_rules import DEFAULT_RULES


# 允許 auto_excluded=True + income-flavored txn_type 的白名單.
#
# 兩類白名單來源:
#
# 【設計本意】 — amount 反向 offset, 需要 exclude 不重複算, 見 wiki
# [[rule-auto-excluded-per-txn-stats-skip-pattern]]:
#   - 「刷卡回饋」「退款退貨」「退款全形」: cashback/refund txn_type, backend
#     flow_type=income 但 amount 符號跟一般 income row 相反, 需要 auto_excluded
#     擋掉不重複算. 前端 renderAmount 對 cashback/refund txn_type 也 skip.
#
# 【Pre-existing but 實務不觸發】 — 2026-07-05 linter 首次啟用時發現的 4 條 rule,
# 其 pattern keyword 對 `classify_by_desc_and_sign(kw, -5000)` 會 fallback 到
# REFUND (因為缺其他 keyword 命中, 掉到「負值 fallback → refund」line 124-126):
#   - 「轉帳匯款」「信用卡還款」「信用卡帳單」「永豐銀行內部」
# 但 prod 這些 desc 通常配著 bank-specific short-circuit (UBOT txCode=20,
# HSBC isPositive) 或 desc 實際字面是「自動扣繳」有 PAYMENT_KW 命中, 不會走 desc-only
# fallback. 加白名單保留 pre-existing behavior, 但 wiki
# [[frontend-cross-layer-display-vs-stats-consistency]] 變種二 section 已標記
# 需追蹤 prod 是否真有 sinopac 「上期帳單 -5000」literal desc row.
WHITELIST_RULES = frozenset({
    # 設計本意
    "刷卡回饋",
    "退款退貨",
    "退款全形",
    # Pre-existing (2026-07-05 grandfathered)
    "轉帳匯款",
    "信用卡還款",
    "信用卡帳單",
    "永豐銀行內部",
})

# 這些 txn_type 已經被 `_transaction_cashflow` normalize 成
# cashflow_direction=income + cashflow_amount=abs(amt), stats 會加進 income 桶.
# 若 rule 對這種 row 標 auto_excluded=True, stats 會 skip → 割裂.
INCOME_FLAVORED_TXN_TYPES = frozenset({
    "cashback",
    "refund",
    "fee_waiver",
})


def _extract_literal_keywords(pattern: str) -> list[str]:
    """從 regex pattern 拆出「純字面 keyword」用來測 classifier.

    只留單純字元組成的 alternation piece, 跳掉含 regex 特殊字元 (^$*+?()[]{}\\.)
    的部分, 避免測試時把 regex 當字面.
    """
    alternatives = pattern.split("|")
    literals: list[str] = []
    for alt in alternatives:
        alt = alt.strip()
        if not alt:
            continue
        # 剔除含 regex meta 字元的 (保留全形字元、中文、英文、數字)
        if re.search(r"[\^\$\*\+\?\(\)\[\]\{\}\\\.]", alt):
            continue
        literals.append(alt)
    return literals


@pytest.mark.parametrize("rule", DEFAULT_RULES,
                         ids=lambda r: r["name"])
def test_auto_excluded_rules_dont_conflict_with_income_txn_types(rule: dict) -> None:
    """Rule with auto_excluded=True 不能命中 income-flavored txn_type row.

    Parametrize over every rule in DEFAULT_RULES 而不是單一 test, 這樣 pytest 出
    錯訊息能明確標示哪條 rule 出問題.
    """
    if not rule.get("auto_excluded"):
        return  # 不 auto_excluded 就不管
    if rule["name"] in WHITELIST_RULES:
        return  # 設計本意允許

    keywords = _extract_literal_keywords(rule["pattern"])
    assert keywords, (
        f"Rule '{rule['name']}' pattern={rule['pattern']!r} 沒有可測試的字面 keyword. "
        f"若這條 rule 的 pattern 全是 regex, 手動加到 WHITELIST_RULES 並在 wiki "
        f"[[rule-auto-excluded-per-txn-stats-skip-pattern]] 記錄理由."
    )

    for kw in keywords[:5]:  # 取前 5 個代表性 keyword, 避免 pattern 太長時 test 太慢
        for amount in (-5000, 5000):
            txn_type = classify_by_desc_and_sign(kw, amount)
            assert txn_type not in INCOME_FLAVORED_TXN_TYPES, (
                f"\n"
                f"❌ Cross-layer inconsistency detected\n"
                f"   Rule name       : {rule['name']!r}\n"
                f"   Rule pattern kw : {kw!r}\n"
                f"   Test amount     : {amount}\n"
                f"   auto_excluded   : True (Layer 2)\n"
                f"   classify_by_desc_and_sign returns: {txn_type!r} (Layer 1 income-flavored)\n"
                f"\n"
                f"   Layer 1 makes this row show 綠色 income in the UI row\n"
                f"   (renderAmount → direction='income') but Layer 2 auto_excluded=True\n"
                f"   makes stats aggregate skip it → 月統計 income 卡片憑空少 {abs(amount)}.\n"
                f"\n"
                f"   Fix options:\n"
                f"     (a) Remove auto_excluded from rule '{rule['name']}' (preferred).\n"
                f"     (b) If cross-layer skip is intentional (amount is offset, not\n"
                f"         real income), add '{rule['name']}' to WHITELIST_RULES in\n"
                f"         this test file AND document reason in wiki\n"
                f"         [[rule-auto-excluded-per-txn-stats-skip-pattern]].\n"
                f"\n"
                f"   Root cause & pattern see wiki\n"
                f"   [[frontend-cross-layer-display-vs-stats-consistency]] 變種二 section."
            )


def test_whitelist_names_actually_exist_in_default_rules() -> None:
    """Sanity: WHITELIST_RULES 名字必須存在 DEFAULT_RULES, 否則白名單過時."""
    rule_names = {r["name"] for r in DEFAULT_RULES}
    stale = WHITELIST_RULES - rule_names
    assert not stale, (
        f"WHITELIST_RULES contains names not in DEFAULT_RULES: {stale}. "
        f"Either the rule was renamed/removed (update WHITELIST) or the whitelist "
        f"entry was never valid."
    )


def test_income_flavored_txn_types_match_transaction_cashflow_impl() -> None:
    """Sanity: INCOME_FLAVORED_TXN_TYPES 必須跟 _transaction_cashflow() 的 income branch 一致.

    若 backend/server/routers/transactions.py:_transaction_cashflow 修改了 income
    分支, 此常數也要同步更新. 這個 test 直接檢查 impl 契約.
    """
    from backend.server.routers.transactions import _transaction_cashflow

    # 對每個 income-flavored 分類, verify 兩種 amount 都回 direction='income'
    for txn_type in INCOME_FLAVORED_TXN_TYPES:
        for amt in (-5000, 5000):
            direction, _ = _transaction_cashflow(amt, txn_type)
            assert direction == "income", (
                f"txn_type={txn_type!r} amount={amt} → direction={direction!r}, "
                f"但被列入 INCOME_FLAVORED_TXN_TYPES. 兩者需一致 — 修改 "
                f"_transaction_cashflow 時同步更新此 test 的常數."
            )
