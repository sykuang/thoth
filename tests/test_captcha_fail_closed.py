from __future__ import annotations

from pathlib import Path

from backend.core import captcha as captcha_mod


class _Element:
    def is_visible(self) -> bool:
        return True

    def screenshot(self, *, path: str) -> None:
        Path(path).write_bytes(b"captcha")


class _Page:
    def query_selector(self, _selector: str) -> _Element:
        return _Element()


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
