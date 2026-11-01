"""Pure offline checks for registered per-action errors without changing exceptions."""
import importlib

import pytest


@pytest.mark.parametrize("form", ["with", "decorator"])
def test_error_step_never_assigns_exception_traceback(form):
    codes = _codes()
    invoked = []

    class TracebackTrap(ValueError):
        def __setattr__(self, key, value):
            if key == "__traceback__":
                invoked.append(key)
                raise RuntimeError("synthetic traceback setter ran")
            return super().__setattr__(key, value)

    original = TracebackTrap("synthetic private source")

    def fail():
        raise original

    try:
        if form == "with":
            with codes.error_step("CTBC-04-010-001"):
                fail()
        else:
            codes.error_step("CTBC-04-010-001")(fail)()
    except BaseException as actual:
        assert actual is original
        assert codes.exception_error_id(actual, bank="ctbc") == "CTBC-04-010-001"
    else:
        raise AssertionError("the source exception was swallowed")
    assert invoked == []


def _codes():
    try:
        return importlib.import_module("backend.core.error_codes")
    except ModuleNotFoundError:
        raise AssertionError("the registered per-action diagnostic carrier is missing") from None


def test_original_exception_and_registered_reason_survive_suppressed_wrapper():
    codes = _codes()
    original = ValueError("synthetic private payload")
    assert codes.with_error_code(original, "CTBC-04-010-001") is original
    try:
        try:
            raise original
        except ValueError:
            raise RuntimeError("synthetic private wrapper") from None
    except RuntimeError as outer:
        assert codes.exception_error_id(outer, bank="ctbc") == "CTBC-04-010-001"
        assert codes.exception_error_id(outer, bank="hsbc") is None
        assert "synthetic private" not in codes.format_error_id("CTBC-04-010-001")


def test_non_exception_class_descriptor_is_not_executed():
    codes = _codes()
    invoked = []

    class ForeignObject:
        @property
        def __class__(self):  # pyright: ignore[reportIncompatibleMethodOverride]
            invoked.append("class")
            return ValueError

    assert codes.exception_error_id(ForeignObject(), bank="ctbc") is None
    assert invoked == []


def test_unknown_code_is_never_exported():
    codes = _codes()
    private = "synthetic private payload"
    error = ValueError(private)
    assert codes.with_error_code(error, private) is error
    assert codes.exception_error_id(error, bank="ctbc") is None
    assert codes.format_error_id(private) is None


def test_nested_action_keeps_the_deeper_original_failure():
    codes = _codes()
    original = TimeoutError("synthetic private timeout")
    try:
        with codes.error_step("CTBC-04-010-002"), codes.error_step("CTBC-04-010-001"):
            raise original
    except TimeoutError as error:
        assert error is original
        assert codes.exception_error_id(error, bank="ctbc") == "CTBC-04-010-001"
    else:
        raise AssertionError("native exception was swallowed")


def test_explicit_cause_wins_over_unrelated_implicit_context():
    codes = _codes()
    primary = codes.with_error_code(ValueError("synthetic primary"), "CTBC-04-010-001")
    unrelated = codes.with_error_code(ValueError("synthetic context"), "CTBC-04-010-002")
    outer = codes.with_error_code(RuntimeError("synthetic wrapper"), "CTBC-04-010-003")
    outer.__cause__ = primary
    outer.__context__ = unrelated
    assert codes.exception_error_id(outer, bank="ctbc") == "CTBC-04-010-001"


def test_hostile_state_keys_and_descriptors_are_never_executed():
    codes = _codes()
    invoked = []

    class HostileKey(str):
        def __hash__(self):
            invoked.append("hash")
            return super().__hash__()

        def __eq__(self, other):
            invoked.append("eq")
            return super().__eq__(other)

    class HostileError(ValueError):
        @property
        def __dict__(self):
            invoked.append("dict")
            raise AssertionError("no descriptors")

        def __str__(self):
            invoked.append("str")
            raise AssertionError("no exception text")

        def __setattr__(self, key, value):
            invoked.append("setattr")
            raise AssertionError("no setters")

    error = HostileError("synthetic private")
    assert codes.with_error_code(error, "CTBC-04-010-001") is error
    assert codes.exception_error_id(error, bank="ctbc") == "CTBC-04-010-001"
    assert invoked == []
    state = BaseException.__dict__["__dict__"].__get__(error, BaseException)
    state[HostileKey("synthetic")] = "synthetic private"
    invoked.clear()
    assert codes.with_error_code(error, "CTBC-04-010-002") is error
    assert codes.exception_error_id(error, bank="ctbc") is None
    assert invoked == []
