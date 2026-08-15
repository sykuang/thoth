from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_card_detail_discloses_missing_post_date_and_fallback() -> None:
    src = (ROOT / "frontend/src/components/transactions/TxnDetailModal.tsx").read_text()
    row_src = (ROOT / "frontend/src/components/transactions/TxnRow.tsx").read_text()
    settings_src = (ROOT / "frontend/src/app/(tabs)/settings/index.tsx").read_text()

    assert "尚無入帳日" in src
    assert "暫按消費日" in src
    assert "（消費日）" in row_src
    assert "銀行尚未提供入帳日時" in settings_src
