#!/usr/bin/env python3
"""CAPTCHA OCR helper - shared by banks with image captcha (HSBC, UBOT, SCSB).

圖形驗證碼 OCR — 共用給有 CAPTCHA 的銀行（HSBC、聯邦等）。

用 ddddocr 本地 OCR（離線、免費、支援 cron 全自動）。
對驗證碼 <img> element 直接 screenshot（座標永遠精準），不裁全頁圖。
失敗時呼叫 regenerate 換一張重試。
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
import threading
from pathlib import Path

# ddddocr singleton 與 thread-safety lock。
# 2026-06-17 C-4 修法：原本 `_OCR` singleton 無 lock，sync_runner 多 worker 並行同一銀行
# 第一次 OCR 時可能同時觸發 `ddddocr.DdddOcr()` init，導致 model weights 競爭加載。
# 加 `_OCR_LOCK` 保護 init 與 inference call (ddddocr 內部 PyTorch / ONNX 非 thread-safe)。
_OCR = None
_OCR_LOCK = threading.Lock()


def _get_ocr():
    global _OCR
    if _OCR is None:
        with _OCR_LOCK:
            if _OCR is None:
                import ddddocr
                _OCR = ddddocr.DdddOcr(show_ad=False)
    return _OCR


def _ocr_classification(data: bytes, *, probability: bool = False):
    """Thread-safe wrapper for ddddocr.classification.

    ddddocr 內部用 PyTorch / ONNX session，並行呼叫會 race。
    所有 inference path 統一過此 lock。
    """
    ocr = _get_ocr()
    with _OCR_LOCK:
        if probability:
            return ocr.classification(data, probability=True)
        return ocr.classification(data)


def _log(*a):
    print(*a, file=sys.stderr)


def _finite_confidence(value: object) -> float | None:
    try:
        confidence = float(str(value))
    except (TypeError, ValueError):
        return None
    return confidence if math.isfinite(confidence) else None


def solve_captcha(
    page,
    img_selector: str,
    *,
    expected_len: int = 5,
    alnum_only: bool = True,
    tmp_path: Path | None = None,
    min_confidence: float = 0.0,
    digits_only: bool = False,
) -> str | None:
    """對 img_selector 指向的驗證碼圖 OCR，回傳辨識字串（清理後）。失敗回 None。

    min_confidence > 0 時，啟用 ddddocr probability，信心低於門檻回 None（觸發換圖重試）。
    digits_only=True 時，OCR 結果非純數字直接判失敗（聯邦驗證碼=純數字 6 碼）。

    tmp_path 保留為相容參數與 caller isolation guard；圖片只在記憶體處理，不落盤。
    """
    if tmp_path is None:
        raise ValueError(
            "solve_captcha() 需要 tmp_path 參數 (per-bank session_dir/captcha.png)。"
            " /tmp 共用 race condition 已禁用——詳見 C-3 修法註解。",
        )
    try:
        el = page.query_selector(img_selector)
        if not el or not el.is_visible():
            _log(f"[captcha] selector {img_selector!r} 不可見")
            return None
        data = el.screenshot()
        if not isinstance(data, bytes):
            return None
        conf = None
        if min_confidence > 0:
            try:
                rp = _ocr_classification(data, probability=True)
                if isinstance(rp, dict):
                    raw = rp.get("text", "")
                    conf = rp.get("confidence")
                else:
                    raw = rp
            except Exception:
                _log("[captcha] probability inference 失敗，confidence gate fail closed")
                return None
        else:
            raw = _ocr_classification(data)
        conf = _finite_confidence(conf)
        text = str(raw).strip()
        if alnum_only:
            text = re.sub(r"[^0-9a-zA-Z]", "", text)
        if digits_only and not text.isdigit():
            _log("[captcha] OCR 非純數字，判失敗重試")
            return None
        if expected_len and len(text) != expected_len:
            _log(f"[captcha] 長度 {len(text)} != 預期 {expected_len}，可能誤判")
            return None
        if min_confidence > 0:
            if conf is None:
                _log("[captcha] 缺少 confidence，捨棄換圖")
                return None
            if conf < min_confidence:
                _log(f"[captcha] 信心 {conf:.3f} < {min_confidence}，捨棄換圖")
                return None
        return text or None
    except Exception:
        _log("[captcha] OCR 失敗")
        return None


def ocr_bytes(
    data: bytes,
    *,
    expected_len: int = 5,
    alnum_only: bool = True,
    digits_only: bool = False,
    min_confidence: float = 0.0,
) -> str | None:
    """直接對圖片位元組做 OCR（給「從 API/DOM 拿 base64」的雙保險路徑用，免截圖）。

    參數語意同 solve_captcha，但輸入是 raw bytes 而非 page+selector。
    """
    try:
        conf = None
        if min_confidence > 0:
            try:
                rp = _ocr_classification(data, probability=True)
                if isinstance(rp, dict):
                    raw = rp.get("text", "")
                    conf = rp.get("confidence")
                else:
                    raw = rp
            except Exception:
                _log("[captcha] probability inference 失敗，confidence gate fail closed")
                return None
        else:
            raw = _ocr_classification(data)
        conf = _finite_confidence(conf)
        text = str(raw).strip()
        if alnum_only:
            text = re.sub(r"[^0-9a-zA-Z]", "", text)
        if digits_only and not text.isdigit():
            return None
        if expected_len and len(text) != expected_len:
            return None
        if min_confidence > 0 and (conf is None or conf < min_confidence):
            return None
        return text or None
    except Exception:
        _log("[captcha] ocr_bytes 失敗")
        return None


def wait_captcha_stable(
    page,
    img_selector: str,
    *,
    tries: int = 6,
    gap_ms: int = 500,
    tmp_path: Path | None = None,
) -> bool:
    """等驗證碼圖渲染穩定（連續兩次 screenshot 位元組相同），避免抓到換圖中途的舊/空圖。

    tmp_path 保留為相容參數與 caller isolation guard；hash 與圖片只留在記憶體。
    """
    if tmp_path is None:
        raise ValueError(
            "wait_captcha_stable() 需要 tmp_path 參數 (per-bank session_dir/captcha.png)。"
            " /tmp 共用 race condition 已禁用——詳見 C-3 修法註解。",
        )
    last = None
    for _ in range(tries):
        el = page.query_selector(img_selector)
        if el and el.is_visible():
            try:
                raw = el.screenshot()
                if not isinstance(raw, bytes):
                    raise TypeError
                h = hashlib.sha256(raw).digest()
                if h == last:
                    return True
                last = h
            except Exception:
                pass
        page.wait_for_timeout(gap_ms)
    return last is not None
