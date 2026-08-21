from __future__ import annotations

from backend.core import captcha as captcha_mod


class _Element:
    def __init__(self) -> None:
        self.screenshot_timeouts: list[int | None] = []

    def is_visible(self) -> bool:
        return True

    def screenshot(
        self,
        *,
        path: str | None = None,
        timeout: int | None = None,
    ) -> bytes:
        assert path is None
        self.screenshot_timeouts.append(timeout)
        return b"captcha"


class _Page:
    def __init__(self) -> None:
        self.element = _Element()

    def query_selector(self, _selector: str) -> _Element:
        return self.element

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


def test_ocr_bytes_rejects_when_probability_inference_fails(monkeypatch) -> None:
    calls: list[bool] = []

    def fake_classification(_data: bytes, *, probability: bool = False):
        calls.append(probability)
        if probability:
            raise RuntimeError("probability unavailable")
        return "ab12"

    monkeypatch.setattr(captcha_mod, "_ocr_classification", fake_classification)

    assert captcha_mod.ocr_bytes(b"image", expected_len=4, min_confidence=0.85) is None
    assert calls == [True]


def test_solve_captcha_rejects_when_confidence_is_missing(tmp_path, monkeypatch) -> None:
    def fake_classification(_data: bytes, *, probability: bool = False):
        assert probability is True
        return {"text": "ab12", "confidence": None}

    monkeypatch.setattr(captcha_mod, "_ocr_classification", fake_classification)

    assert captcha_mod.solve_captcha(
        _Page(),
        "img",
        expected_len=4,
        min_confidence=0.85,
        tmp_path=tmp_path / "captcha.png",
    ) is None


def test_confidence_gate_rejects_nan_for_both_ocr_paths(tmp_path, monkeypatch) -> None:
    def fake_classification(_data: bytes, *, probability: bool = False):
        assert probability is True
        return {"text": "ab12", "confidence": float("nan")}

    monkeypatch.setattr(captcha_mod, "_ocr_classification", fake_classification)

    assert captcha_mod.ocr_bytes(
        b"image",
        expected_len=4,
        min_confidence=0.85,
    ) is None
    assert captcha_mod.solve_captcha(
        _Page(),
        "img",
        expected_len=4,
        min_confidence=0.85,
        tmp_path=tmp_path / "captcha.png",
    ) is None


def test_captcha_helpers_never_log_plaintext_raw_or_exception_and_do_not_persist(
    tmp_path, monkeypatch, capsys
) -> None:
    marker = "PRIVATECAPTCHA987654"

    monkeypatch.setattr(
        captcha_mod,
        "_ocr_classification",
        lambda _data, *, probability=False: {
            "text": marker,
            "confidence": 0.99,
        },
    )
    target = tmp_path / "captcha.png"
    assert captcha_mod.ocr_bytes(
        b"PRIVATE-RAW-IMAGE",
        expected_len=len(marker),
        min_confidence=0.85,
    ) == marker
    assert captcha_mod.solve_captcha(
        _Page(),
        "img",
        expected_len=len(marker),
        min_confidence=0.85,
        tmp_path=target,
    ) == marker
    assert not target.exists()
    assert "PRIVATE" not in capsys.readouterr().err

    def fail(_data: bytes, *, probability: bool = False):
        raise RuntimeError("PRIVATE-OCR-EXCEPTION-987654")

    monkeypatch.setattr(captcha_mod, "_ocr_classification", fail)
    assert captcha_mod.ocr_bytes(b"image", min_confidence=0.85) is None
    assert captcha_mod.solve_captcha(
        _Page(),
        "img",
        min_confidence=0.85,
        tmp_path=target,
    ) is None
    assert "PRIVATE" not in capsys.readouterr().err


def test_wait_captcha_stable_hashes_screenshots_in_memory(tmp_path) -> None:
    page = _Page()
    target = tmp_path / "stable.png"
    assert captcha_mod.wait_captcha_stable(
        page,
        "img",
        tries=2,
        gap_ms=0,
        tmp_path=target,
    )
    assert page.element.screenshot_timeouts == [5000, 5000]
    assert not target.exists()


def test_solve_captcha_bounds_element_screenshot_timeout(tmp_path, monkeypatch) -> None:
    page = _Page()
    monkeypatch.setattr(
        captcha_mod,
        "_ocr_classification",
        lambda _data, *, probability=False: "123456",
    )

    assert captcha_mod.solve_captcha(
        page,
        "img",
        expected_len=6,
        tmp_path=tmp_path / "captcha.png",
    ) == "123456"
    assert page.element.screenshot_timeouts == [5000]
