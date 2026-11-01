"""Pinned diagnostic source fixtures must fail closed, never use candidate HEAD."""
import subprocess

import pytest

from tests import diagnostic_originals as originals


@pytest.mark.parametrize("path", sorted(originals.SHA256))
def test_original_source_has_verified_immutable_bytes(path):
    assert originals.original_source(path)


def test_missing_original_is_not_a_skip(monkeypatch):
    originals.original_source.cache_clear()

    def missing(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0])

    monkeypatch.setattr(subprocess, "check_output", missing)
    with pytest.raises(RuntimeError, match="Pinned diagnostic baseline is unavailable"):
        originals.original_source("backend/core/base.py")


def test_changed_original_is_rejected(monkeypatch):
    originals.original_source.cache_clear()
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: b"synthetic wrong source")
    with pytest.raises(ValueError, match="Pinned diagnostic baseline hash mismatch"):
        originals.original_source("backend/core/base.py")


@pytest.mark.parametrize("filename", sorted(originals.AUTH_SHA256))
def test_auth_original_source_has_verified_immutable_bytes(filename):
    assert originals.auth_original_source(filename)


@pytest.mark.parametrize("corrupt", [False, True])
def test_auth_originals_cannot_be_missing_or_changed(monkeypatch, corrupt):
    from pathlib import Path
    originals.auth_original_source.cache_clear()

    def read(_path):
        if corrupt:
            return b"synthetic wrong source"
        raise FileNotFoundError("synthetic missing original")

    monkeypatch.setattr(Path, "read_bytes", read)
    expected = ValueError if corrupt else RuntimeError
    with pytest.raises(expected, match="Pinned auth baseline"):
        originals.auth_original_source("base.py")


def test_unknown_source_is_rejected_before_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unknown original must not reach git")

    monkeypatch.setattr(subprocess, "check_output", forbidden)
    with pytest.raises(KeyError):
        originals.original_source("backend/.env")
