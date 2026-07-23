"""Regression: iOS Release build should not emit known avoidable Xcode warnings.

Covers warning classes observed during `pnpm expo run:ios --configuration Release`:
- ambiguous shell script dependencies for the Expo Dev Launcher strip script
- duplicate `-lc++` linker flag from target-level OTHER_LDFLAGS plus Pods xcconfig
- dSYM/module-cache `.pcm` missing warnings caused by Release dSYM generation in local installs
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PBXPROJ = ROOT / "frontend/ios/Thoth.xcodeproj/project.pbxproj"


def _phase_block(src: str, phase_name: str) -> str:
    marker = f"/* {phase_name} */ = {{"
    start = src.index(marker)
    end = src.index("\n\t\t};", start)
    return src[start:end]


def _build_config_block(src: str, config_name: str, occurrence: int = 1) -> str:
    marker = f"/* {config_name} */ = {{"
    start = -1
    search_from = 0
    for _ in range(occurrence):
        start = src.index(marker, search_from)
        search_from = start + 1
    end = src.index("\n\t\t};", start)
    return src[start:end]


def test_expo_dev_launcher_strip_script_has_explicit_dependency_policy():
    src = PBXPROJ.read_text()
    block = _phase_block(src, "[Expo Dev Launcher] Strip Local Network Keys for Release")
    assert "alwaysOutOfDate = 1;" in block


def test_app_target_does_not_add_duplicate_libcxx_linker_flag():
    src = PBXPROJ.read_text()
    # First two Debug/Release config blocks are the Thoth app target configs.
    debug = _build_config_block(src, "Debug", occurrence=1)
    release = _build_config_block(src, "Release", occurrence=1)
    assert '"-lc++"' not in debug
    assert '"-lc++"' not in release


def test_release_build_uses_dwarf_not_dsym_for_local_device_installs():
    src = PBXPROJ.read_text()
    # The app target Release config should override the project default.
    release = _build_config_block(src, "Release", occurrence=1)
    assert "DEBUG_INFORMATION_FORMAT = dwarf;" in release
