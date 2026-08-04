from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "frontend/src/app/(tabs)/dashboard.tsx"


def test_passive_income_card_uses_current_month_and_ytd_scopes() -> None:
    src = DASHBOARD.read_text(encoding="utf-8")
    start = src.index("function PassiveIncomeCard")
    card = src[start:src.index("// PortfolioHeader", start)]

    assert "currentMonthKey" in card
    assert "currentYear" in card
    assert "byMonth[currentMonthKey]" in card
    assert "amountByMonth[currentMonthKey]?.income" in card
    assert "month.startsWith(`${currentYear}-`)" in card
    assert "stats.passive_income_total" not in card
    assert "stats.passive_income_pct" not in card
