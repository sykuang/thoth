"""One safe failure sink for CLI and queued jobs; never inspect raw error text."""
from backend.core.error_codes import bank_bound_error_id, bank_error_id, exception_error_id, format_error_id


class CollectDiagnosticError(RuntimeError):
    def __init__(self, crawler, diagnostics):
        super().__init__("crawler_failed")
        self.crawler = crawler
        self.diagnostics = diagnostics


class FailureDiagnosticError(CollectDiagnosticError):
    """Login/browser terminal carrier, separately validated at each boundary."""


def raise_crawler_failure(crawler, result):
    from backend.core.base import _validated_collect_diagnostics, _validated_failure_diagnostics

    diagnostics = _validated_collect_diagnostics(crawler, result.get("collect_diagnostics"))
    if diagnostics:
        raise CollectDiagnosticError(crawler, diagnostics)
    diagnostics = _validated_failure_diagnostics(crawler, result.get("failure_diagnostics"))
    if diagnostics:
        raise FailureDiagnosticError(crawler, diagnostics)
    raise RuntimeError("crawler_failed")


def safe_failure_message(error, bank, *, persistence=False):
    """Revalidate carriers, then prefer deepest registered original-cause ID."""
    from backend.core.error_codes import code_details

    message = None
    if type(error) is CollectDiagnosticError or type(error) is FailureDiagnosticError:
        try:
            from backend.core.base import (
                _validated_collect_diagnostics, _format_collect_diagnostics,
                _validated_failure_diagnostics, _format_failure_diagnostics,
            )
            validate, render = (
                (_validated_failure_diagnostics, _format_failure_diagnostics)
                if type(error) is FailureDiagnosticError else
                (_validated_collect_diagnostics, _format_collect_diagnostics)
            )
            diagnostics = validate(error.crawler, error.diagnostics)
            # Both the crawler binding and the actual job binding must agree.
            if diagnostics and ("error_id" not in diagnostics or code_details(diagnostics["error_id"], bank=bank)):
                message = render(diagnostics)
                if "error_id" not in diagnostics:
                    prefix = format_error_id(bank_error_id(bank, "07-960-001"))
                    message = f"{prefix} | {message}"
        except Exception:
            pass
    if message:
        return message
    error_id = bank_bound_error_id(bank, exception_error_id(error, bank=bank))
    error_id = error_id or bank_error_id(bank, "08-900-001" if persistence else "09-900-001")
    prefix = format_error_id(error_id, bank=bank) or format_error_id("SYS-09-900-001")
    family = "Exception"
    try:
        from backend.core.base import _safe_exception_type
        cause = BaseException.__dict__["__cause__"].__get__(error, BaseException) if persistence else error
        family = _safe_exception_type(cause) if cause is not None else "Exception"
    except Exception:
        pass
    return f"{prefix} | sync_failed:{family}" + (": phase=persistence" if persistence else "")
