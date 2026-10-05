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
    expected = "qc.invalidateQueries({ queryKey: ['frontend-dataset'] });"

    assert expected in _sync_completion_effect(DASHBOARD.read_text())
    assert expected in _sync_completion_effect(ACCOUNTS_TAB.read_text())


def test_payment_reminders_recompute_locally_on_foreground_and_midnight() -> None:
    """Wiring guard; mounted React probes separately exercise the callback delivery."""
    dashboard = DASHBOARD.read_text()
    layout = ROOT_LAYOUT.read_text()

    assert "/cards/auto-debit/reminders" not in dashboard
    assert "datasetQ.data?.paymentReminderInputs, paymentDay" in dashboard
    assert "const updateDay = () => setPaymentDay(taipeiPaymentDay());" in dashboard
    assert "focusManager.subscribe(focused => { if (focused) updateDay(); })" in dashboard
    timer_start = dashboard.index("const timer = setInterval(() => {")
    timer_end = dashboard.index("}, 60_000);", timer_start)
    assert "updateDay();" in dashboard[timer_start:timer_end]
    assert "clearInterval(timer); unsubscribe();" in dashboard
    assert "focusManager" in layout
    assert "AppState.addEventListener('change'" in layout
    assert "focusManager.setFocused(status === 'active')" in layout
