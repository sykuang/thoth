from pathlib import Path


INFO_PLIST = Path(__file__).resolve().parents[1] / "frontend/ios/Thoth/Info.plist"


def test_ios_info_plist_uses_xcode_version_sources() -> None:
    text = INFO_PLIST.read_text()
    assert "<string>$(MARKETING_VERSION)</string>" in text
    assert "<string>$(CURRENT_PROJECT_VERSION)</string>" in text
