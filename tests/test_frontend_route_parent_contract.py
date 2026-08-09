"""Every visible Expo Router page has one deterministic parent contract."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "frontend/src/app"
MANIFEST = ROOT / "frontend/src/lib/routeParents.ts"
BACK_BUTTON = ROOT / "frontend/src/components/DeterministicBackButton.tsx"

ROOTS = {
    "login",
    "(tabs)/dashboard",
    "(tabs)/transactions",
    "(tabs)/cards/index",
    "(tabs)/settings/index",
}
REDIRECTS = {"index", "(tabs)/investments"}
PARENTS = {
    "+not-found": "/",
    "(tabs)/cards/add": "/(tabs)/cards",
    "(tabs)/cards/new": "/(tabs)/cards/add",
    "(tabs)/cards/credentials/[bank]": "/(tabs)/cards",
    "(tabs)/cards/[bank]/[card_no]": "/(tabs)/cards",
    "(tabs)/cards/brokerage/[account_id]": "/(tabs)/cards",
    "(tabs)/cards/manual/[account_id]": "/(tabs)/cards",
    "(tabs)/cards/manual/transaction": "/(tabs)/cards/manual/[account_id]",
    "(tabs)/settings/categories": "/(tabs)/settings",
    "(tabs)/settings/labels": "/(tabs)/settings",
    "(tabs)/settings/auto-sync": "/(tabs)/settings",
}
DYNAMIC = {
    "(tabs)/cards/manual/[account_id]": "manualAccountParent",
    "(tabs)/cards/manual/transaction": "manualTransactionReturnParent",
}


def _route(path: Path) -> str:
    return path.relative_to(APP).with_suffix("").as_posix()


def _code(path: Path) -> str:
    source = path.read_text()
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\n]*", "", source)


def _manifest_parents() -> dict[str, str]:
    source = MANIFEST.read_text()
    body = re.search(r"export const ROUTE_PARENTS = \{(.*?)\} as const;", source, re.S)
    assert body
    return dict(re.findall(r"'([^']+)': '([^']+)'", body.group(1)))


def test_comment_decoys_do_not_satisfy_live_wiring(tmp_path: Path) -> None:
    source = tmp_path / "decoy.tsx"
    source.write_text("/* router.dismissTo(target) */\nonPress={() => router.push(target)}\n")
    assert "dismissTo" not in _code(source)


def test_route_inventory_and_parent_graph() -> None:
    assert _manifest_parents() == PARENTS
    pages = {
        _route(path)
        for path in APP.rglob("*.tsx")
        if path.name not in {"_layout.tsx", "+native-intent.tsx"}
    }
    classified = ROOTS | REDIRECTS | set(PARENTS)
    assert pages == classified, (
        f"missing={sorted(pages - classified)}, stale={sorted(classified - pages)}"
    )

    href_to_route = {f"/{route.removesuffix('/index')}": route for route in classified}
    href_to_route["/"] = "index"
    assert all(parent in href_to_route for parent in PARENTS.values())
    for route in PARENTS:
        seen = {route}
        current = route
        while current in PARENTS:
            current = href_to_route[PARENTS[current]]
            assert current not in seen, f"parent cycle: {route} -> {current}"
            seen.add(current)
        assert current in ROOTS | REDIRECTS

    for route in ROOTS:
        assert "<Redirect href=" not in (APP / f"{route}.tsx").read_text()
    for route in REDIRECTS:
        assert "<Redirect href=" in (APP / f"{route}.tsx").read_text()


def test_visible_back_controls_are_deterministic() -> None:
    button = _code(BACK_BUTTON)
    assert "onPress={() => router.dismissTo(target)}" in button
    assert 'accessibilityRole="button"' in button
    assert "accessibilityLabel={`返回${label}`}" in button
    assert "router.back()" not in button

    for route in PARENTS:
        source = _code(APP / f"{route}.tsx")
        assert "router.back()" not in source, route
        if "dismissTo(" in source:
            assert "@/lib/routeParents" in source, route
            helper = DYNAMIC.get(route)
            assert f"ROUTE_PARENTS['{route}']" in source or (
                helper is not None and helper in source
            )

    not_found = _code(APP / "+not-found.tsx")
    assert "ROUTE_PARENTS['+not-found']" in not_found
    assert 'accessibilityLabel="返回首頁"' in not_found


def test_stack_and_dynamic_parent_wiring() -> None:
    cards = _code(APP / "(tabs)/cards/_layout.tsx")
    settings = _code(APP / "(tabs)/settings/_layout.tsx")
    for layout, prefix in ((cards, "(tabs)/cards/"), (settings, "(tabs)/settings/")):
        assert "headerBackVisible: false" in layout
        assert "headerLeft: () => <DeterministicBackButton" in layout
        assert "target={ROUTE_PARENTS[route]}" in layout
        for route in PARENTS:
            if not route.startswith(prefix):
                continue
            local_name = route.removeprefix(prefix)
            assert f'name="{local_name}"' in layout
            if route not in DYNAMIC:
                assert f"options={{screen('{route}'" in layout

    manual = _code(APP / "(tabs)/cards/manual/[account_id].tsx")
    transaction = _code(APP / "(tabs)/cards/manual/transaction.tsx")
    for source, helper in (
        (manual, "manualAccountParent"),
        (transaction, "manualTransactionReturnParent"),
    ):
        assert "<Stack.Screen" in source
        assert "headerLeft:" in source
        assert helper in source

    assert "options={screen('(tabs)/cards/manual/[account_id]'" in cards
    assert "const parent = manualAccountParent(accountId)" in manual
    assert "target={parent}" in manual
    assert "label={isNew ? '新增帳戶' : '帳戶'}" in manual
    assert "const returnTarget = manualTransactionReturnParent(" in transaction
    assert "target={returnTarget}" in transaction
    assert "routeIsValidated" in transaction
    assert "transactionIdIsValid && initialTransaction != null" in transaction
