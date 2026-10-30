"""Four remaining new emissions must preserve old optional decisions."""
from copy import deepcopy
from importlib import import_module

import pytest

from tests.test_optional_diagnostic_logging import Page, original_namespace


@pytest.mark.parametrize("mode", ["logger", "formatter"])
@pytest.mark.parametrize("reason,code", [
    ("paid", "DBS-06-001-001"),
    ("due", "DBS-06-002-001"),
    ("mismatch", "DBS-06-003-001"),
])
def test_dbs_optional_fact_rejection_survives_diagnostic_failure(monkeypatch, mode, reason, code):
    module = import_module("backend.banks.dbs")
    old = original_namespace("dbs")["_dbs_card_bill_fact"]
    payment = {"amount": "100", "alreadyPaid": "10", "dueDate": "2026-09-10"}
    fee = {"bill_due_amount": "100", "payment_due_date": "2026-09-10"}
    if reason == "paid":
        payment.pop("alreadyPaid")
    elif reason == "due":
        fee.pop("payment_due_date")
        payment.pop("amount")  # The independent dashboard fallback is incomplete too.
    else:
        payment["dueDate"] = "2026-09-11"
    data = {"api_responses": {"liabilities": [{"resp": {"creditCard": {"paymentDetails": payment}}}]},
            "dbs_card_fee_page": fee}
    retained = deepcopy(data)
    assert old(data) is None
    calls = []

    def fail(value, **kwargs):
        calls.append(value)
        raise BrokenPipeError("synthetic diagnostic failure")

    monkeypatch.setattr(module, "_log" if mode == "logger" else "format_error_id", fail)
    assert module._dbs_card_bill_fact(data) is None
    assert data == retained
    assert len(calls) == 1
    assert calls[0].startswith(f"[{code}] ") if mode == "logger" else calls[0] == code


@pytest.mark.parametrize("mode", ["logger", "formatter"])
@pytest.mark.parametrize("native", ["goto", "wait"])
@pytest.mark.parametrize("authenticated", [False, True])
def test_scb_optional_prepare_recovery_survives_diagnostic_failure(monkeypatch, mode, native, authenticated):
    module = import_module("backend.banks.scb")
    old_class = original_namespace("scb")["ScbCrawler"]
    before = Page("scb", (native, 1))
    old = object.__new__(old_class)
    old._logged_in = lambda page: authenticated
    assert old.prepare_login_page(before) is None
    page = Page("scb", (native, 1))
    current = object.__new__(module.ScbCrawler)
    current._logged_in = lambda page: authenticated
    calls = []

    def fail(value, **kwargs):
        calls.append(value)
        raise BrokenPipeError("synthetic diagnostic failure")

    monkeypatch.setattr(module, "_log" if mode == "logger" else "format_error_id", fail)
    assert current.prepare_login_page(page) is None
    assert page.events == before.events
    assert len(calls) == 1
    expected = "SCB-01-001-001" if native == "goto" else "SCB-01-002-001"
    assert calls[0].startswith(f"[{expected}] ") if mode == "logger" else calls[0] == expected


def test_new_optional_emission_does_not_swallow_base_exception(monkeypatch):
    module = import_module("backend.banks.dbs")

    class Cancel(BaseException):
        pass

    def cancel(*args, **kwargs):
        raise Cancel

    monkeypatch.setattr(module, "_log", cancel)
    with pytest.raises(Cancel):
        module._dbs_card_bill_fact({"dbs_card_fee_page": {"bill_due_amount": "100"}})
