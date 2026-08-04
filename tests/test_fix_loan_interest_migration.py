from migrations.fix_loan_interest_20260804 import _should_repair


def _row(**overrides):
    row = {
        "description": "放款利息",
        "expend": 38395,
        "income": None,
        "category": "利息股息",
        "flow_type": "income",
        "income_category": "interest_dividend",
        "description_overwrite": None,
        "tags_overwrite": None,
    }
    row.update(overrides)
    return row


def test_migration_repairs_only_misclassified_loan_interest_expense() -> None:
    assert _should_repair(_row()) is True
    assert _should_repair(_row(description="存款利息")) is False
    assert _should_repair(_row(expend=None, income=31)) is False
    assert _should_repair(_row(flow_type="expense", income_category=None)) is False


def test_migration_preserves_user_edited_rows() -> None:
    assert _should_repair(_row(description_overwrite="房貸利息")) is False
    assert _should_repair(_row(tags_overwrite='["mortgage"]')) is False
