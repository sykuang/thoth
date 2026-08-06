"""Regression guards for dashboard payment-reminder cache freshness."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "frontend/src/app/(tabs)/dashboard.tsx"
ACCOUNTS_TAB = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
ROOT_LAYOUT = ROOT / "frontend/src/app/_layout.tsx"


def _sync_completion_effect(source: str) -> str:
    start = source.index("if (prevHasRunningRef.current && !")
    end = source.index("prevHasRunningRef.current =", start)
    return source[start:end]


def test_sync_completion_invalidates_payment_reminders_everywhere() -> None:
    """A bank sync changes due dates/amounts, so every sync observer must evict reminders."""
    expected = "qc.invalidateQueries({ queryKey: ['auto-debit', 'reminders'] });"

    assert expected in _sync_completion_effect(DASHBOARD.read_text())
    assert expected in _sync_completion_effect(ACCOUNTS_TAB.read_text())


def test_payment_reminders_refetch_when_native_app_returns_to_foreground() -> None:
    """A date-derived reminder cached before midnight must not survive app resume."""
    dashboard = DASHBOARD.read_text()
    layout = ROOT_LAYOUT.read_text()

    query_start = dashboard.index("const remindersQ = useQuery<PaymentReminder[]>")
    query_end = dashboard.index("});", query_start)
    reminder_query = dashboard[query_start:query_end]

    assert "refetchOnWindowFocus: 'always'" in reminder_query
    assert "focusManager" in layout
    assert "AppState.addEventListener('change'" in layout
    assert "focusManager.setFocused(status === 'active')" in layout
