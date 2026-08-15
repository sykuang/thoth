"""Regression: account/card drilldown must use canonical ids, not last4.

Last-four matching is ambiguous (two accounts/cards can share a suffix) and the
backend Transaction shape already has the canonical row identity available from
bank DB rows. The UI should route exact account_no/card_no and filter exact
fields, while still displaying only masked `account_or_card`.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSACTIONS_ROUTER_PY = ROOT / "backend/server/routers/transactions.py"
API_TS = ROOT / "frontend/src/types/api.ts"
CARDS_INDEX_TSX = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
TRANSACTIONS_TSX = ROOT / "frontend/src/app/(tabs)/transactions.tsx"


def test_backend_transaction_shape_exposes_canonical_account_and_card_refs():
    src = TRANSACTIONS_ROUTER_PY.read_text()

    assert '"account_no": account_no' in src
    assert '"card_no": _row_get(r, "card_no")' in src
    # Display field remains masked; canonical refs are for internal filtering only.
    assert '"account_or_card": _mask_tail(account_no)' in src
    assert '"account_or_card": _mask_tail(_row_get(r, "card_no"))' in src


def test_frontend_types_include_canonical_refs_without_changing_display_field():
    src = API_TS.read_text()

    assert "account_no?: string | null;" in src
    assert "card_no?: string | null;" in src
    assert "account_or_card: string | null;" in src


def test_account_tab_routes_canonical_refs_not_last_four():
    src = CARDS_INDEX_TSX.read_text()
    account_row = src[src.index("function AccountRow"):src.index("function CardRow")]
    card_row = src[src.index("function CardRow"):src.index("// ============================================================\n// Helpers")]

    assert "account_no: account.account_no" in account_row
    assert "account_tail: account.account_no.slice(-4)" not in account_row
    assert "card_no: card.card_no" in card_row
    assert "account_tail: card.card_no.slice(-4)" not in card_row


def test_transactions_tab_filters_by_exact_canonical_ref():
    src = TRANSACTIONS_TSX.read_text()

    assert "account_no?: string; card_no?: string; drilldown?: string" in src
    assert "brokerage_account_id" not in src
    assert "const [activeAccountNo, setActiveAccountNo] = useState(accountNo);" in src
    assert "const [activeCardNo, setActiveCardNo] = useState(cardNo);" in src
    assert "t.account_no === effectiveAccountNo" in src
    assert "matchesCardDrilldown(t, effectiveCardNo)" in src
    assert "account_tail" not in src


def test_transactions_drilldown_route_params_are_part_of_local_dataset_filter():
    src = TRANSACTIONS_TSX.read_text()

    assert "useFrontendDatasetCache()" in src
    assert "let items = datasetQ.data?.transactions ?? [];" in src
    assert "if (effectiveAccountNo) items = items.filter((t) => t.account_no === effectiveAccountNo);" in src
    assert "if (effectiveCardNo) items = items.filter((t) => matchesCardDrilldown(t, effectiveCardNo));" in src
    assert "[datasetQ.data, selectedBanks, effectiveAccountNo, effectiveCardNo, granularity, selectedPeriod, cardDateBasis]" in src


def test_backend_transactions_endpoint_supports_exact_canonical_ref_filters():
    src = TRANSACTIONS_ROUTER_PY.read_text()

    assert "account_no: str | None = Query(None" in src
    assert "card_no: str | None = Query(None" in src
    assert "items = [t for t in items if t.get(\"account_no\") == account_no]" in src
    assert 't.get("card_no") == card_no' in src
    assert 't.get("kind") in {"billed", "pending"} and not t.get("card_no")' in src
