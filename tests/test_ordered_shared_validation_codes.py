"""Synthetic, offline rejection fixtures for the real shared validators."""
from copy import deepcopy
from typing import Any

import pytest

from backend.core import base
from backend.core.error_codes import BANKS, code_details, exception_error_id


@pytest.mark.parametrize("function,value,code", [
    ("_require_iso_date", 7, "980-001"),
    ("_require_iso_date", "20260101", "980-002"),
    ("_require_iso_date", "2026-02-30", "980-003"),
    ("_require_iso_date_or_datetime", 7, "981-001"),
    ("_require_iso_date_or_datetime", "20260101", "981-002"),
    ("_require_iso_date_or_datetime", "2026-01-01T10:20+24:00", "981-003"),
    ("_require_iso_date_or_datetime", "2026-01-01T10:20+00:60", "981-004"),
    ("_require_iso_date_or_datetime", "2026-02-30T10:20", "981-005"),
    ("_require_iso_date_or_datetime", "2026-01-01T25:20", "981-005"),
    ("_history_date", 7, "982-001"),
    ("_history_date", "20260101", "982-002"),
    ("_history_date", "2026-02-30", "982-003"),
])
def test_dates_ordered_leaf_codes(function, value, code):
    with pytest.raises(ValueError) as caught:
        if function == "_history_date":
            base._history_date(value, "invalid history coverage")
        else:
            getattr(base, function)(value, path="synthetic.date")
    assert type(caught.value) is ValueError
    assert exception_error_id(caught.value) == f"SYS-07-{code}"
    if function == "_history_date":
        assert str(caught.value) == "invalid history coverage"
    else:
        kind = "IsoDate YYYY-MM-DD" if function == "_require_iso_date" else "IsoDate/ISO datetime"
        assert str(caught.value) == f"synthetic.date must be {kind}, got {value!r}"


@pytest.mark.parametrize("value", [None, "", "2024-01-01", "2035-01-01", "2024-02-29"])
def test_optional_and_outside_window_dates_still_accepted(value):
    base._require_iso_date(value, path="synthetic.date")
    base._require_iso_date_or_datetime(value, path="synthetic.date")


@pytest.mark.parametrize("value", ["2026-01-01T23:59:59Z", "2026-01-01 10:20+23:59", "2026-01-01T10:20-2359"])
def test_datetime_success_policy(value):
    base._require_iso_date_or_datetime(value, path="synthetic.datetime")


def _fact(**changes):
    return {"scope": "card", "card_no": "synthetic-card", "status": "unpaid", "remaining_due": 1, **changes}


CARD_CASES = [
    ([], True, "983-001", "card_bill_facts_ok=True requires at least one fact", ValueError),
    ([_fact()], False, "983-002", "card_bill_facts require card_bill_facts_ok=True", ValueError),
    ([7], True, "983-003", "card_bill_facts[0] must be a dict", ValueError),
    ([_fact(extra=1)], True, "983-004", "card_bill_facts[0] has unknown fields: ['extra']", ValueError),
    ([_fact(scope="other")], True, "983-005", "card_bill_facts[0].scope must be 'bank' or 'card'", ValueError),
    ([_fact(card_no=7)], True, "983-006", "card_bill_facts[0].card_no is required for card scope", ValueError),
    ([_fact(card_no=" ")], True, "983-007", "card_bill_facts[0].card_no is required for card scope", ValueError),
    ([_fact(), _fact()], True, "983-008", "card_bill_facts[1].card_no is duplicated", ValueError),
    ([_fact(scope="bank")], True, "983-009", "card_bill_facts[0].card_no is forbidden for bank scope", ValueError),
    ([_fact(status="other")], True, "983-010", "card_bill_facts[0].status is not canonical", ValueError),
    ([_fact(status="unpaid", remaining_due=0)], True, "983-016", "card_bill_facts[0] status conflicts with remaining_due", ValueError),
    ([_fact(status="paid")], True, "983-017", "card_bill_facts[0] status conflicts with remaining_due", ValueError),
    ([_fact(last_payment_date="2026-01-01")], True, "983-018", "card_bill_facts[0] last payment must be an atomic pair", ValueError),
    ([_fact(last_payment_amount=1)], True, "983-019", "card_bill_facts[0] last payment must be an atomic pair", ValueError),
    ([_fact(), _fact(scope="bank", card_no=None)], True, "983-025", "card_bill_facts must be one bank fact or card-scoped facts", ValueError),
    ([_fact(scope="bank", card_no=None)] * 2, True, "983-026", "card_bill_facts must be one bank fact or card-scoped facts", ValueError),
    (None, None, "984-001", "'NoneType' object is not iterable", TypeError),
    ([_fact(scope=[])], True, "984-002", "unhashable type: 'list'", TypeError),
    ([_fact(status=[])], True, "984-003", "unhashable type: 'list'", TypeError),
    ([_fact(remaining_due=10 ** 400)], True, "984-004", "int too large to convert to float", OverflowError),
    ([_fact(last_payment_amount=10 ** 400, last_payment_date="2026-01-01")], True, "984-005", "int too large to convert to float", OverflowError),
    ([{**_fact(), 1: 1, "extra": 1}], True, "984-006", ("'<' not supported between instances of 'str' and 'int'", "'<' not supported between instances of 'int' and 'str'"), TypeError),
]
for field, start in [("remaining_due", 11), ("last_payment_amount", 20)]:
    for offset, value in enumerate([True, "1", float("nan"), -1, 100_000_001]):
        changes = {field: value}
        if field == "last_payment_amount":
            changes["last_payment_date"] = "2026-01-01"
        CARD_CASES.append(([_fact(**changes)], True, f"983-{start + offset:03}",
                           f"card_bill_facts[0].{field} must be finite and non-negative", ValueError))


@pytest.mark.parametrize("facts,facts_ok,code,message,error_type", CARD_CASES)
def test_card_bill_actual_rejections(facts, facts_ok, code, message, error_type):
    with pytest.raises(error_type) as caught:
        base.validate_card_bill_facts(deepcopy(facts), facts_ok=facts_ok)
    assert type(caught.value) is error_type
    assert str(caught.value) in (message if isinstance(message, tuple) else (message,))
    assert exception_error_id(caught.value) == f"SYS-07-{code}"


@pytest.mark.parametrize("field,start", [("statement_close_date", 101), ("payment_due_date", 104), ("last_payment_date", 107)])
@pytest.mark.parametrize("value,reason", [(7, 0), ("compact", 1), ("2026-02-30", 2)])
def test_card_bill_date_linkage(field, start, value, reason):
    fact = _fact(**{field: value})
    if field == "last_payment_date":
        fact["last_payment_amount"] = 1
    with pytest.raises(ValueError) as caught:
        base.validate_card_bill_facts([fact], facts_ok=True)
    assert type(caught.value) is ValueError
    _assert_final_date_diagnostic(caught.value, f"SYS-07-983-{start + reason:03}")
    assert str(caught.value) == f"card_bill_facts[0].{field} must be IsoDate YYYY-MM-DD, got {value!r}"


def test_card_bill_existing_success_policy():
    base.validate_card_bill_facts([], facts_ok=None)
    base.validate_card_bill_facts((), facts_ok=False)  # No new list-only policy.
    base.validate_card_bill_facts([_fact(remaining_due=0.5)], facts_ok=True)
    for status in ("paid", "no_payment_required"):
        base.validate_card_bill_facts([_fact(status=status, remaining_due=0,
                                          last_payment_amount=0, last_payment_date="",
                                          payment_due_date="2035-01-01")], facts_ok=True)


def _coverage(variant="normal"):
    expected = {"identity": "synthetic-account", "start": "2026-01-01", "end": "2026-01-31"}
    window = {**expected, "status": "complete", "pages": 1}
    domain = {"domain": "twd_transactions", "expected": [expected], "windows": [window]}
    if variant == "empty":
        domain.update(expected=[], windows=[], empty_window={
            "start": "2026-01-01", "end": "2026-01-31", "status": "explicit_empty", "pages": 1})
    if variant == "split":
        domain["windows"] = [{**window, "end": "2026-01-15"}, {**window, "start": "2026-01-16"}]
    return {"mode": "full", "domains": [domain]}


_DELETE = object()


def _changed(value, path, replacement):
    if not path:
        return replacement
    target = value
    for key in path[:-1]:
        target = target[key]
    if replacement is _DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = deepcopy(replacement)
    return value


D = ("domains", 0)
E = (*D, "expected", 0)
W = (*D, "windows", 0)
Z = (*D, "empty_window",)
HISTORY_CASES = [
    ("normal", (), None, "985-005"),
    ("normal", ("mode",), "incremental", "985-006"),
    ("normal", ("domains",), None, "985-007"),
    ("normal", ("domains",), [], "985-008"),
    ("normal", D, None, "985-009"),
    ("normal", (*D, "domain",), "other", "985-010"),
    ("normal", (*D, "expected",), None, "985-011"),
    ("normal", (*D, "windows",), None, "985-012"),
    ("normal", (*D, "domain",), [], "985-014"),
    ("empty", Z, None, "986-001"),
    ("empty", (*D, "windows",), [{}], "986-002"),
    ("empty", (*Z, "start",), _DELETE, "986-003"),
    ("empty", (*Z, "end",), _DELETE, "986-004"),
    ("empty", (*Z, "start",), "2026-02-01", "986-005"),
    ("empty", (*Z, "status",), "complete", "986-006"),
    ("empty", (*Z, "pages",), True, "986-007"),
    ("empty", (*Z, "pages",), 0, "986-008"),
    ("normal", E, None, "987-001"),
    ("normal", (*E, "start",), _DELETE, "987-002"),
    ("normal", (*E, "end",), _DELETE, "987-003"),
    ("normal", (*E, "identity",), 7, "987-004"),
    ("normal", (*E, "identity",), " ", "987-005"),
    ("normal", (*D, "expected",), _coverage()["domains"][0]["expected"] * 2, "987-006"),
    ("normal", (*E, "start",), "2026-02-01", "987-007"),
    ("normal", W, None, "988-001"),
    ("normal", (*W, "identity",), "other", "988-002"),
    ("normal", (*W, "start",), _DELETE, "988-003"),
    ("normal", (*W, "end",), _DELETE, "988-004"),
    ("normal", (*W, "start",), "2026-02-01", "988-005"),
    ("normal", (*W, "status",), "failed", "988-006"),
    ("normal", (*W, "pages",), True, "988-007"),
    ("normal", (*W, "pages",), 0, "988-008"),
    ("normal", (*W, "identity",), [], "988-009"),
    ("normal", (*W, "status",), [], "988-010"),
    ("normal", (*D, "windows",), [], "989-001"),
    ("normal", (*W, "start",), "2026-01-02", "989-002"),
    ("split", (*D, "windows", 1, "start"), "2026-01-17", "989-003"),
    ("split", (*D, "windows", 1, "start"), "2026-01-15", "989-004"),
    ("normal", (*W, "end",), "2026-01-30", "989-005"),
    ("normal", ("domains",), _coverage()["domains"] * 2, "989-006"),
    ("normal", (*D, "domain",), "account_transactions", "989-007"),
    ("normal", (*D, "windows",), [
        {**_coverage()["domains"][0]["windows"][0], "end": "9999-12-31"},
        {"identity": "synthetic-account", "start": "9999-12-31", "end": "9999-12-31", "status": "complete", "pages": 1},
    ], "989-008"),
]
for prefix, variant, group in [(Z, "empty", "986"), (E, "normal", "987"), (W, "normal", "988")]:
    for field, start in [("start", 101), ("end", 104)]:
        for value, reason in [(7, 0), ("compact", 1), ("2026-02-30", 2)]:
            HISTORY_CASES.append((variant, (*prefix, field,), value, f"{group}-{start + reason:03}"))


@pytest.mark.parametrize("variant,path,replacement,code", HISTORY_CASES)
def test_history_actual_rejection_leaves(variant, path, replacement, code):
    coverage = _changed(_coverage(variant), path, replacement)
    error_type = TypeError if code in {"985-014", "988-009", "988-010"} else OverflowError if code == "989-008" else ValueError
    with pytest.raises(error_type) as caught:
        base.validate_history_coverage(coverage, expected_mode="full", expected_domains=frozenset({"twd_transactions"}))
    assert type(caught.value) is error_type
    if error_type is ValueError:
        assert str(caught.value) == "invalid history coverage"
    _assert_final_date_diagnostic(caught.value, f"SYS-07-{code}")


@pytest.mark.parametrize("mode,domains,code,error_type", [
    ("other", frozenset({"twd_transactions"}), "985-001", ValueError),
    ("full", {"twd_transactions"}, "985-002", ValueError),
    ("full", frozenset(), "985-003", ValueError),
    ("full", frozenset({"other"}), "985-004", ValueError),
    ([], frozenset({"twd_transactions"}), "985-013", TypeError),
])
def test_history_validator_contract(mode, domains, code, error_type):
    with pytest.raises(error_type) as caught:
        base.validate_history_coverage(_coverage(), expected_mode=mode, expected_domains=domains)
    assert type(caught.value) is error_type
    assert exception_error_id(caught.value) == f"SYS-07-{code}"


@pytest.mark.parametrize("variant", ["normal", "empty", "split"])
def test_history_success_still_preserves_summary(variant):
    summary = base.validate_history_coverage(_coverage(variant), expected_mode="full", expected_domains=frozenset({"twd_transactions"}))
    assert summary == {"ok": True, "mode": "full", "domains": ["twd_transactions"],
                       "identities": 0 if variant == "empty" else 1,
                       "windows": 2 if variant == "split" else 1,
                       "start": "2026-01-01", "end": "2026-01-31"}


def test_status_comparison_is_not_replayed_for_diagnostics():
    comparisons = []

    class Status(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            comparisons.append(other)
            return super().__eq__(other)

    with pytest.raises(ValueError) as caught:
        base.validate_card_bill_facts([_fact(status=Status("unpaid"), remaining_due=0)], facts_ok=True)
    assert exception_error_id(caught.value) == "SYS-07-983-016"
    assert comparisons == ["unpaid", "unpaid"]  # Membership then the original status predicate.


@pytest.mark.parametrize("variant,call_index,code", [
    ("empty", 1, "986-003"), ("empty", 2, "986-004"),
    ("normal", 1, "987-002"), ("normal", 2, "987-003"),
    ("normal", 3, "988-003"), ("normal", 4, "988-004"),
])
@pytest.mark.parametrize("error_type", [ValueError, TypeError, RuntimeError])
def test_history_native_date_failure_injection_keeps_type_and_order(monkeypatch, variant, call_index, code, error_type):
    original = base._history_date
    calls = []
    failure = error_type("synthetic failure")

    def read_once(value, error, **context):
        calls.append(value)
        if len(calls) == call_index:
            raise failure
        return original(value, error, **context)

    monkeypatch.setattr(base, "_history_date", read_once)
    translated_type = RuntimeError if error_type is RuntimeError else ValueError
    with pytest.raises(translated_type) as caught:
        base.validate_history_coverage(_coverage(variant), expected_mode="full", expected_domains=frozenset({"twd_transactions"}))
    assert type(caught.value) is translated_type
    assert len(calls) == call_index
    assert exception_error_id(caught.value) == f"SYS-07-{code}"
    if error_type is RuntimeError:
        assert caught.value is failure
    else:
        assert str(caught.value) == "invalid history coverage"
        assert caught.value.__context__ is failure
        assert caught.value.__suppress_context__ is True


def test_shared_templates_resolve_for_every_supported_bank():
    from backend.core.error_catalog.validation import CODES

    assert len(set(CODES.values())) == len(CODES)
    for error_id, details in CODES.items():
        assert code_details(error_id) == details
        for bank in BANKS:
            qualified = error_id.replace("SYS-", f"{bank.upper()}-", 1)
            assert code_details(qualified, bank=bank) == details


def test_rejection_precedence_remains_short_circuited():
    # Later malformed fields must not override earlier rejection reasons.
    with pytest.raises(ValueError) as caught:
        base.validate_card_bill_facts([_fact(card_no=7, status=[], remaining_due="bad")], facts_ok=True)
    assert exception_error_id(caught.value) == "SYS-07-983-006"
    value = _coverage()
    value["domains"][0]["expected"][0].update(identity=[], start="bad", end="also bad")
    with pytest.raises(ValueError) as caught:
        base.validate_history_coverage(value, expected_mode="full", expected_domains=frozenset({"twd_transactions"}))
    assert exception_error_id(caught.value) == "SYS-07-987-102"


class _DateDiagnosticCrawler(base.BankCrawler):
    def login(self, page):
        raise AssertionError("offline name-only stub")

    def collect(self, page, collector):
        raise AssertionError("offline name-only stub")


def _assert_final_date_diagnostic(error, expected):
    crawler = object.__new__(_DateDiagnosticCrawler)
    crawler.name = "esun"
    assert exception_error_id(error) == expected
    diagnostics = base._collect_failure_diagnostics(crawler, error)
    assert diagnostics["error_id"] == expected.replace("SYS-", "ESUN-", 1)
    assert diagnostics["exception"] == ("Exception" if type(error) is OverflowError else type(error).__name__)
    rendered = repr(diagnostics) + repr(code_details(expected))
    for private in ("synthetic", "compact", "2026-02-30", "[0]", "statement_close_date", "account_date"):
        assert private not in rendered


NORMALIZED_DATE_ROLES = [
    ("accounts", "raw_balance_date", 101),
    ("cards", "statement_close_date", 104),
    ("cards", "payment_due_date", 107),
    ("cards", "last_payment_date", 110),
    ("twd_txns", "account_date", 113),
    ("card_billed_txns", "bill_date", 116),
    ("card_billed_txns", "date", 119),
    ("card_billed_txns", "post_date", 122),
    ("card_pending_txns", "date", 125),
    ("card_pending_txns", "post_date", 128),
    ("balance_history", "snapshotDate", 131),
]


@pytest.mark.parametrize("collection,field,start", NORMALIZED_DATE_ROLES)
@pytest.mark.parametrize("value,reason", [(7, 0), ("compact", 1), ("2026-02-30", 2)])
def test_normalized_actual_date_roles(collection, field, start, value, reason):
    payload: dict[str, Any] = {collection: [{field: value}]}
    with pytest.raises(ValueError) as caught:
        base.BankCollectResult(**payload)
    assert type(caught.value) is ValueError
    assert str(caught.value) == f"{collection}[0].{field} must be IsoDate YYYY-MM-DD, got {value!r}"
    _assert_final_date_diagnostic(caught.value, f"SYS-07-980-{start + reason:03}")


@pytest.mark.parametrize("value,reason", [
    (7, 101), ("compact", 102), ("2026-01-01T10:20+24:00", 103),
    ("2026-01-01T10:20+00:60", 104), ("2026-02-30T10:20", 105),
    ("2026-02-30", 105),
])
def test_normalized_actual_transaction_datetime_role(value, reason):
    with pytest.raises(ValueError) as caught:
        base.BankCollectResult(twd_txns=[{"datetime": value}])
    assert type(caught.value) is ValueError
    assert str(caught.value) == f"twd_txns[0].datetime must be IsoDate/ISO datetime, got {value!r}"
    _assert_final_date_diagnostic(caught.value, f"SYS-07-981-{reason}")
