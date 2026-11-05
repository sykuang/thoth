"""Synthetic legacy-to-current replay; no bank/network or production data."""
import pytest
from backend.banks.cathay import CathayCrawler
from backend.core.persist import persist_collected
from backend.core.store import BankStore


@pytest.mark.parametrize('field', ['postingDate', 'beginValueDate', 'valueDateTime'])
@pytest.mark.parametrize('occurrences', [1, 2])
def test_legacy_midnight_posting_replay_preserves_ids_and_edits(field, occurrences, tmp_path, monkeypatch):
    monkeypatch.setenv('BANK_DATA_ROOT', str(tmp_path))
    monkeypatch.delenv('DB_BACKEND', raising=False)
    store = BankStore('cathay', user_id=7, source_account_id=91)
    try:
        legacy = {'card_no': '9000****1234', 'date': '2026-07-01T00:00:00',
                  'post_date': '2026-07-03T00:00:00', 'desc': 'Synthetic format replay',
                  'amount': 100, 'currency': 'TWD', 'consume_currency': 'TWD',
                  'consume_amount': None, 'txn_type': 'spending'}
        store.upsert_card_billed([dict(legacy) for _ in range(occurrences)])
        ids = [r['id'] for r in store.conn.execute('SELECT id FROM card_billed_txns ORDER BY id')]
        edit_originals(store)
        store.conn.execute("UPDATE card_billed_txns SET category='Original custom' WHERE id=?", (ids[0],))
        store.conn.commit()
        original_rows = rows(store)
        raw = {'cardNo': '9000000000001234', 'consumeDate': legacy['date'],
               'transDesc': legacy['desc'], 'amount': 100, 'currency': 'TWD',
               field: legacy['post_date']}
        parsed = CathayCrawler.__new__(CathayCrawler)._norm_card_txn(raw)
        assert parsed['card_no'] == legacy['card_no']
        data = {'credit_card': {'billed_detail': {'TWD': [dict(parsed) for _ in range(occurrences)]}}}
        persist_collected('cathay', data, store)
        actual = store.conn.execute('SELECT id,post_date,category FROM card_billed_txns ORDER BY id').fetchall()
        assert [r['id'] for r in actual] == ids, 'Equivalent posting-date representations created duplicate ledger rows'
        assert actual[0]['category'] == 'Original custom'
        assert rows(store) == original_rows
        persist_collected('cathay', data, store)
        assert rows(store) == original_rows
    finally:
        store.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('BANK_DATA_ROOT', str(tmp_path))
    monkeypatch.delenv('DB_BACKEND', raising=False)
    value = BankStore('cathay', user_id=7, source_account_id=91)
    try:
        yield value
    finally:
        value.close()


BASE = {'card_no': '****1234', 'date': '2026-07-01',
        'post_date': '2026-07-03', 'desc': 'Synthetic format replay',
        'amount': 100, 'currency': 'TWD', 'consume_currency': 'TWD',
        'consume_amount': None, 'txn_type': 'spending'}
OVERLAYS = ('category', 'subcategory', 'description_overwrite', 'tags_overwrite',
            'auto_excluded', 'splits_overwrite')


def rows(store):
    return [dict(row) for row in store.conn.execute('SELECT * FROM card_billed_txns ORDER BY id')]


def edit_originals(store):
    for i, row in enumerate(rows(store)):
        edits = (f'Custom {i}', f'Sub {i}', f'Note {i}', f'["tag{i}"]', 1,
                 '[{"amount":40,"category":"A"},{"amount":60,"category":"B"}]')
        store.conn.execute(
            f'UPDATE card_billed_txns SET {", ".join(field + "=?" for field in OVERLAYS)} WHERE id=?',
            (*edits, row['id']))
    store.conn.commit()
    return rows(store)


@pytest.mark.parametrize('old,new', [('2026-07-03T00:00:00', '2026-07-03'),
                                    ('2026-07-03', '2026-07-03T00:00:00')])
@pytest.mark.parametrize('count', [1, 2])
def test_bidirectional_posting_replay_preserves_all_original_bytes(store, old, new, count):
    store.upsert_card_billed([{**BASE, 'post_date': old}] * count)
    before = edit_originals(store)
    for posting in (new, old, new):
        assert store.upsert_card_billed([{**BASE, 'post_date': posting}] * count) == 0
        assert rows(store) == before
        assert store._current_billed_ids[-count:] == [row['id'] for row in before]


@pytest.mark.parametrize('old,new', [('2026-07-03T00:00:00', '2026-07-03'),
                                    ('2026-07-03', '2026-07-03T00:00:00')])
def test_replay_can_add_a_genuine_second_occurrence_without_moving_first_edits(store, old, new):
    store.upsert_card_billed([{**BASE, 'post_date': old}])
    before = edit_originals(store)[0]
    assert store.upsert_card_billed([{**BASE, 'post_date': new}] * 2) == 1
    assert rows(store)[0] == before
    assert len(rows(store)) == 2
    assert all(rows(store)[1][field] in (None, 0) for field in OVERLAYS)
    ids = [row['id'] for row in rows(store)]
    assert store.upsert_card_billed([{**BASE, 'post_date': new}] * 2) == 0
    assert [row['id'] for row in rows(store)] == ids
    assert rows(store)[0] == before


@pytest.mark.parametrize('posting', ['2026-07-04', '2026-07-04T00:00:00',
                                    '2026-07-03T00:00:01', '2026-07-03T12:34:56',
                                    '2026-07-03T00:00:00+08:00', '2026-07-03T00:00:00Z'])
def test_posting_equivalence_does_not_truncate_real_times_or_other_dates(store, posting):
    store.upsert_card_billed([BASE])
    before = edit_originals(store)[0]
    assert store.upsert_card_billed([{**BASE, 'post_date': posting}]) == 1
    assert len(rows(store)) == 2
    assert rows(store)[0] == before


@pytest.mark.parametrize('changed', [{'card_no': '****5678'}, {'desc': 'Other purchase'},
                                     {'date': '2026-07-02'}, {'amount': 101}, {'txn_type': 'purchase'},
                                     {'consume_currency': 'USD', 'consume_amount': 3}])
def test_equivalent_posting_does_not_merge_other_purchase_evidence(store, changed):
    store.upsert_card_billed([BASE])
    before = edit_originals(store)[0]
    assert store.upsert_card_billed([{**BASE, **changed, 'post_date': '2026-07-03T00:00:00'}]) == 1
    assert len(rows(store)) == 2
    assert rows(store)[0] == before


def test_equivalent_posting_is_user_scoped(store):
    store.upsert_card_billed([BASE])
    before = edit_originals(store)[0]
    other = BankStore('cathay', user_id=8, source_account_id=92)
    try:
        assert other.upsert_card_billed([{**BASE, 'post_date': '2026-07-03T00:00:00'}]) == 1
        other.conn.commit()
        assert rows(store)[0] == before
        assert [row['user_id'] for row in rows(store)] == [7, 8]
    finally:
        other.close()


def test_equivalent_posting_keeps_currency_conflict_veto(store):
    store.upsert_card_billed([BASE])
    before = edit_originals(store)[0]
    assert store.upsert_card_billed([{**BASE, 'currency': 'USD',
                                     'post_date': '2026-07-03T00:00:00'}]) == 1
    assert rows(store)[0] == before
    assert [row['currency'] for row in rows(store)] == ['TWD', 'USD']


def test_both_existing_representations_are_not_consolidated_or_reedited(store):
    store.upsert_card_billed([BASE])
    # Model already-existing duplicate rows without invoking the candidate matcher.
    original = rows(store)[0]
    fields = [field for field in original if field != 'id']
    duplicate = {**original, 'post_date': '2026-07-03T00:00:00', 'dedup_key': original['dedup_key'].replace('2026-07-03', '2026-07-03T00:00:00')}
    store.conn.execute(
        f'INSERT INTO card_billed_txns ({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})',
        tuple(duplicate[field] for field in fields))
    before = edit_originals(store)
    assert store.upsert_card_billed([BASE]) == 0
    assert store.upsert_card_billed([{**BASE, 'post_date': '2026-07-03T00:00:00'}]) == 0
    assert rows(store) == before


@pytest.mark.parametrize('native_side', ['incoming', 'existing', 'both'])
def test_legacy_posting_equivalence_does_not_relax_native_validation(store, native_side):
    original = {**BASE, **({'native_identity': 'native-one'} if native_side != 'incoming' else {})}
    store.upsert_card_billed([original])
    before = edit_originals(store)
    incoming = {**BASE, 'post_date': '2026-07-03T00:00:00',
                **({'native_identity': 'native-one'} if native_side != 'existing' else {})}
    if incoming.get('native_identity'):
        with pytest.raises(ValueError, match='incomplete native card occurrence'):
            store.upsert_card_billed([incoming])
    else:
        assert store.upsert_card_billed([incoming]) == 1
    assert rows(store)[:1] == before


@pytest.mark.parametrize('old_card,new_card', [('', '****1234'), ('****1234', '')])
def test_card_backfill_and_blank_statement_replay_use_same_posting_equivalence(store, old_card, new_card):
    store.upsert_card_billed([{**BASE, 'card_no': old_card, 'post_date': '2026-07-03T00:00:00'}])
    before = edit_originals(store)[0]
    assert store.upsert_card_billed([{**BASE, 'card_no': new_card}]) == 0
    after = rows(store)
    assert len(after) == 1
    assert after[0] == {**before, 'card_no': '****1234'}


def test_booked_blank_card_ambiguity_sees_equivalent_legacy_posting(store):
    store.upsert_card_billed([BASE, {**BASE, 'card_no': '****5678',
                                   'post_date': '2026-07-03T00:00:00'}])
    before = edit_originals(store)
    with pytest.raises(ValueError, match='ambiguous blank-card booking identity'):
        store.refresh_card_pending('current', [{**BASE, 'card_no': '', 'booking_status': 'booked'}],
                                   fetch_ok=True)
    assert rows(store) == before


@pytest.mark.parametrize('reverse', [False, True])
def test_mixed_serialization_occurrences_reserve_their_exact_key_targets(store, reverse):
    midnight = {**BASE, 'post_date': '2026-07-03T00:00:00'}
    store.upsert_card_billed([midnight])
    before = edit_originals(store)[0]
    batch = [BASE, midnight]
    if reverse:
        batch.reverse()
    assert store.upsert_card_billed(batch) == 1
    assert len(rows(store)) == 2
    assert rows(store)[0] == before
    snapshot = rows(store)
    assert store.upsert_card_billed(batch) == 0
    assert rows(store) == snapshot


@pytest.mark.parametrize('reverse', [False, True])
def test_format_replay_and_distinct_new_purchase_share_batch_without_edit_transfer(store, reverse):
    store.upsert_card_billed([{**BASE, 'post_date': '2026-07-03T00:00:00'}])
    before = edit_originals(store)[0]
    batch = [BASE, {**BASE, 'desc': 'Independent new purchase'}]
    if reverse:
        batch.reverse()
    assert store.upsert_card_billed(batch) == 1
    assert rows(store)[0] == before
    assert len(rows(store)) == 2
    assert rows(store)[1]['description'] == 'Independent new purchase'
    assert all(rows(store)[1][field] in (None, 0) for field in OVERLAYS)


@pytest.mark.parametrize('route', ['direct', 'batch', 'promotion'])
@pytest.mark.parametrize('reverse', [False, True])
def test_edited_blank_history_cannot_be_claimed_by_first_of_two_cards(store, route, reverse):
    blank = {**BASE, 'card_no': '', 'post_date': '2026-07-03T00:00:00'}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([blank, independent])
    before = edit_originals(store)
    batch = [BASE, {**BASE, 'card_no': '****5678'}, independent]
    if reverse:
        batch.reverse()
    if route == 'direct':
        store.upsert_card_billed(batch)
    elif route == 'batch':
        store.persist_card_batch(batch, [], scope='current', fetch_ok=True)
    else:
        store.refresh_card_pending('current', [{**txn, 'booking_status': 'booked'} for txn in batch],
                                   fetch_ok=True)
    actual = rows(store)
    assert actual[:2] == before, 'Ambiguous blank-card history was assigned to the first incoming card'
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


OWNER_ROUTES = [
    (route, old, new)
    for route in ('direct', 'batch')
    for old, new in [('2026-07-03T00:00:00', '2026-07-03'),
                     ('2026-07-03', '2026-07-03T00:00:00'),
                     ('2026-07-03', '2026-07-03')]
] + [('promotion', old, '2026-07-03')
     for old in ('2026-07-03T00:00:00', '2026-07-03')]


def apply_owner_batch(store, batch, route):
    if route == 'direct':
        store.upsert_card_billed(batch)
    elif route == 'batch':
        store.persist_card_batch(batch, [], scope='current', fetch_ok=True)
    else:
        store.refresh_card_pending('current', [{**txn, 'booking_status': 'booked'} for txn in batch],
                                   fetch_ok=True)


@pytest.mark.parametrize('route,old,new', OWNER_ROUTES)
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('exact_sibling', [False, True])
def test_complete_incoming_card_class_cannot_claim_blank_history(store, route, old, new, reverse, exact_sibling):
    blank = {**BASE, 'card_no': '', 'post_date': old}
    exact = {**BASE, 'card_no': '****5678', 'post_date': new}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([blank, independent, *([exact] if exact_sibling else [])])
    before = edit_originals(store)
    batch = [{**BASE, 'post_date': new}, exact, independent,
             {**independent, 'desc': 'Independent new purchase'}]
    if reverse:
        batch.reverse()
    apply_owner_batch(store, batch, route)
    actual = rows(store)
    assert actual[:len(before)] == before, 'Complete incoming class must own blank-card eligibility'
    assert any(row['description'] == 'Independent new purchase' for row in actual[len(before):])
    assert all(row[field] in (None, 0) for row in actual[len(before):] for field in OVERLAYS)


@pytest.mark.parametrize('route,old,new', [case for case in OWNER_ROUTES if case[0] != 'promotion'])
@pytest.mark.parametrize('reverse', [False, True])
def test_blank_incoming_cannot_choose_between_known_historical_cards(store, route, old, new, reverse):
    known = [{**BASE, 'card_no': card, 'post_date': old} for card in ('****1234', '****5678')]
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([*known, independent])
    before = edit_originals(store)
    batch = [{**BASE, 'card_no': '', 'post_date': new},
             {**known[0], 'post_date': new}, independent,
             {**independent, 'desc': 'Independent new purchase'}]
    if reverse:
        batch.reverse()
    apply_owner_batch(store, batch, route)
    actual = rows(store)
    assert actual[:len(before)] == before
    assert any(row['description'] == 'Independent new purchase' for row in actual[len(before):])
    by_id = {row['id']: row for row in actual}
    for target_id, sources in store._billed_source_rows.items():
        if target_id in {row['id'] for row in before[:2]}:
            assert all(source['card_no'] == by_id[target_id]['card_no'] for source in sources), \
                'Blank source was assigned an edited known-card target without unique ownership'
    assert all(row[field] in (None, 0) for row in actual[len(before):] for field in OVERLAYS)


@pytest.mark.parametrize('route', ['direct', 'batch', 'promotion'])
@pytest.mark.parametrize('reverse', [False, True])
def test_undated_blank_statement_backfill_sees_later_incoming_card(store, route, reverse):
    historical = {**BASE, 'post_date': None}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([historical, independent])
    before = edit_originals(store)
    batch = [{**BASE, 'card_no': ''}, {**BASE, 'card_no': '****5678'}, independent,
             {**independent, 'desc': 'Independent new purchase'}]
    if reverse:
        batch.reverse()
    apply_owner_batch(store, batch, route)
    actual = rows(store)
    assert actual[:2] == before, 'Preparation must not infer ownership before seeing the full batch'
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('route,old,new', OWNER_ROUTES)
@pytest.mark.parametrize('blank_side', ['existing', 'incoming'])
def test_unique_card_class_retains_genuine_occurrences_and_all_edits(store, route, old, new, blank_side):
    old_card = '' if blank_side == 'existing' else BASE['card_no']
    new_card = BASE['card_no'] if blank_side == 'existing' else ''
    store.upsert_card_billed([{**BASE, 'card_no': old_card, 'post_date': old}] * 2)
    before = edit_originals(store)
    apply_owner_batch(store, [{**BASE, 'card_no': new_card, 'post_date': new}] * 2, route)
    assert rows(store) == [{**row, 'card_no': BASE['card_no']} for row in before]


@pytest.mark.parametrize('route', ['direct', 'batch', 'promotion'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('changed', [
    {'currency': 'USD'}, {'consume_currency': 'USD'}, {'consume_amount': 3},
    {'txn_type': 'purchase'}, {'desc': 'Different purchase'}, {'date': '2026-07-02'},
    {'desc': '\tSynthetic format replay\t'},
    {'post_date': '2026-07-04'}, {'post_date': '2026-07-03T00:00:01'},
])
def test_other_card_vetoes_do_not_block_unique_blank_backfill(store, route, reverse, changed):
    blank = {**BASE, 'card_no': '', 'post_date': '2026-07-03T00:00:00'}
    other = {**BASE, 'card_no': '****5678', **changed}
    # Promotion intentionally refuses non-calendar timestamps; keep that case on
    # its real pending route while still exercising the canonical booked sibling.
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([blank, other, independent])
    before = edit_originals(store)
    batch = [BASE, other, independent]
    if reverse:
        batch.reverse()
    apply_owner_batch(store, batch, route)
    assert rows(store) == [{**before[0], 'card_no': BASE['card_no']}, *before[1:]]


@pytest.mark.parametrize('old', ['2026-07-03T00:00:00', '2026-07-03'])
@pytest.mark.parametrize('reverse', [False, True])
def test_blank_incoming_promotion_keeps_existing_ambiguity_rollback(store, old, reverse):
    known = [{**BASE, 'card_no': card, 'post_date': old} for card in ('****1234', '****5678')]
    store.upsert_card_billed(known)
    before = edit_originals(store)
    batch = [{**BASE, 'card_no': ''}, {**BASE, 'desc': 'Independent new purchase'}]
    if reverse:
        batch.reverse()
    with pytest.raises(ValueError, match='ambiguous blank-card booking identity'):
        apply_owner_batch(store, batch, 'promotion')
    assert rows(store) == before


@pytest.mark.parametrize('billed_card,booked_card', [('****1234', '****5678'),
                                                   ('****5678', '****1234')])
@pytest.mark.parametrize('reverse', [False, True])
def test_owning_batch_card_class_spans_billed_and_booked_partitions(store, billed_card, booked_card, reverse):
    blank = {**BASE, 'card_no': '', 'post_date': '2026-07-03T00:00:00'}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([blank, independent])
    before = edit_originals(store)
    billed = [{**BASE, 'card_no': billed_card}, independent]
    booked = [{**BASE, 'card_no': booked_card, 'booking_status': 'booked'},
              {**independent, 'desc': 'Independent new purchase', 'booking_status': 'booked'}]
    if reverse:
        billed.reverse()
        booked.reverse()
    store.persist_card_batch(billed, booked, scope='current', fetch_ok=True)
    actual = rows(store)
    assert actual[:2] == before, 'Split billed/booked inputs assigned ambiguous historical edits'
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('reverse', [False, True])
def test_blank_booked_source_excludes_incompatible_currency_owner(store, reverse):
    other = {**BASE, 'card_no': '****5678', 'currency': 'USD',
             'post_date': '2026-07-03T00:00:00'}
    store.upsert_card_billed([BASE, other])
    before = edit_originals(store)
    booked = [{**BASE, 'card_no': '', 'booking_status': 'booked'},
              {**BASE, 'card_no': '****9999', 'desc': 'Independent new purchase',
               'booking_status': 'booked'}]
    if reverse:
        booked.reverse()
    store.refresh_card_pending('current', booked, fetch_ok=True)
    actual = rows(store)
    assert actual[:2] == before
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('changed', [
    {'consume_currency': 'USD'}, {'consume_amount': 3}, {'txn_type': 'purchase'},
    {'native_identity': 'other-native', 'post_date': '2026-07-03'},
    {'date': '2026-07-02'}, {'post_date': '2026-07-03T00:00:01'},
    {'post_date': '2026-07-03T00:00:00+08:00'},
])
def test_blank_booking_owner_count_uses_all_immutable_vetoes(store, reverse, changed):
    store.upsert_card_billed([BASE])
    original = rows(store)[0]
    other = {**original, 'card_no': '****5678', 'post_date': '2026-07-03T00:00:00',
             'dedup_key': 'synthetic-v3-other-owner', **changed}
    if 'date' in changed:
        other['consume_date'] = changed['date']
    fields = list(original.keys() - {'id'})
    store.conn.execute(
        f'INSERT INTO card_billed_txns ({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})',
        tuple(other[field] for field in fields))
    assert len(rows(store)) == 2, 'The ambiguity fixture must contain two actual historical rows'
    before = edit_originals(store)
    incoming = [{**BASE, 'card_no': '', 'booking_status': 'booked'},
                {**BASE, 'desc': 'Independent new purchase', 'booking_status': 'booked'}]
    if reverse:
        incoming.reverse()
    if changed.get('native_identity'):
        # Missing native evidence is not a contradictory known reference. This
        # canonical native sibling remains a genuine competing owner, not a veto.
        with pytest.raises(ValueError, match='ambiguous blank-card booking identity'):
            store.refresh_card_pending('current', incoming, fetch_ok=True)
        assert rows(store) == before
        return
    store.refresh_card_pending('current', incoming, fetch_ok=True)
    actual = rows(store)
    assert actual[:2] == before
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('fetch_ok,status,posting', [
    (False, 'booked', '2026-07-03'), (None, 'booked', '2026-07-03'),
    (True, None, '2026-07-03'), (True, 'pending', '2026-07-03'),
    (True, 'booked', '2026-07-03T00:00:00'), (True, 'booked', '2026-02-30'),
])
def test_unadmitted_pending_cannot_claim_or_veto_billed_blank_owner(store, reverse, fetch_ok, status, posting):
    blank = {**BASE, 'card_no': '', 'post_date': '2026-07-03T00:00:00'}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([blank, independent])
    before = edit_originals(store)
    billed = [BASE, independent, {**independent, 'desc': 'Independent new purchase'}]
    if reverse:
        billed.reverse()
    store.persist_card_batch(billed, [{**BASE, 'card_no': '****5678', 'booking_status': status,
                                       'post_date': posting}], scope='current', fetch_ok=fetch_ok)
    actual = rows(store)
    assert actual[:2] == [{**before[0], 'card_no': BASE['card_no']}, before[1]]
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('reverse', [False, True])
def test_owning_batch_reserves_exact_keys_across_billed_booked_split(store, reverse):
    midnight = {**BASE, 'post_date': '2026-07-03T00:00:00'}
    # Booked accepts only a calendar date, so the existing exact key is canonical.
    store.upsert_card_billed([BASE])
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([independent])
    before = edit_originals(store)
    billed = [midnight, independent]
    booked = [{**BASE, 'amount': '100.0', 'currency': 'twd'},
              {**independent, 'desc': 'Independent new purchase'}]
    if reverse:
        billed.reverse()
        booked.reverse()
    store.persist_card_batch(billed, [{**txn, 'booking_status': 'booked'} for txn in booked],
                             scope='current', fetch_ok=True)
    actual = rows(store)
    assert actual[:2] == before
    same = [row for row in actual if row['description'] == BASE['desc']]
    assert len(same) == 2, 'Two source occurrences must not consume the same historical occurrence'
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('competing_card', [False, True])
@pytest.mark.parametrize('raw_side,money,currency,posting', [
    ('billed', '100', 'twd', ''), ('booked', '100.0', '', '2026-07-03'),
    ('billed', '100.0', None, '2026-07-03'),
    ('booked', 'decimal', 'twd', '2026-07-03'),
])
def test_split_ownership_uses_writer_compatible_money_and_defaults(store, reverse, competing_card, raw_side, money, currency, posting):
    from decimal import Decimal
    blank = {**BASE, 'card_no': '', 'post_date': '2026-07-03T00:00:00'}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([blank, independent])
    before = edit_originals(store)
    billed = [dict(BASE), independent]
    booked = [{**BASE, 'card_no': '****5678' if competing_card else BASE['card_no'], 'booking_status': 'booked'},
              {**independent, 'desc': 'Independent new purchase', 'booking_status': 'booked'}]
    source = billed[0] if raw_side == 'billed' else booked[0]
    source.update(amount=Decimal('100.00') if money == 'decimal' else money,
                  currency=currency, post_date=posting)
    if reverse:
        billed.reverse()
        booked.reverse()
    store.persist_card_batch(billed, booked, scope='current', fetch_ok=True)
    actual = rows(store)
    assert actual[:2] == [before[0] if competing_card else {**before[0], 'card_no': BASE['card_no']}, before[1]]
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('bad_side,value,expected,suppressed', [
    ('booked', 'bad', 'CATHAY-08-865-004', True),
    ('billed', 'bad', 'CATHAY-08-865-004', True),
    ('billed', '100.5', 'CATHAY-08-865-007', False),
    ('overflow', '1e9', 'CATHAY-08-865-010', False),
])
def test_split_evidence_keeps_native_money_error_order_and_decimal_context(store, bad_side, value, expected, suppressed):
    from decimal import localcontext, Overflow
    from backend.core.error_codes import exception_error_id
    from backend.core.persist._common import _snapshot_occurrence_ledgers
    store.upsert_card_billed([BASE])
    before = edit_originals(store)
    ledgers = _snapshot_occurrence_ledgers(store)
    billed = [{**BASE, 'desc': 'Independent new purchase'}]
    booked = [{**BASE, 'card_no': '****5678', 'booking_status': 'booked'}]
    if bad_side == 'billed':
        billed[0]['amount'] = value
        booked[0]['amount'] = 'bad'
    else:
        booked[0]['amount'] = value
    with localcontext() as context:
        context.clear_flags()
        if bad_side == 'overflow':
            context.Emax = 3
        traps = dict(context.traps)
        with pytest.raises(Overflow if bad_side == 'overflow' else ValueError) as caught:
            store.persist_card_batch(billed, booked, scope='current', fetch_ok=True)
        assert dict(context.traps) == traps
        flags = {signal.__name__ for signal, active in context.flags.items() if active}
        assert flags == ({'Overflow', 'Inexact', 'Rounded'} if bad_side == 'overflow' else
                         {'InvalidOperation'} if value == 'bad' else set())
    assert exception_error_id(caught.value) == expected
    assert caught.value.__suppress_context__ is suppressed
    if bad_side != 'overflow':
        assert caught.value.args == ('invalid native monetary value',)
    assert rows(store) == before
    assert _snapshot_occurrence_ledgers(store) == ledgers


@pytest.mark.parametrize('scope', ['current', 'unbilled'])
@pytest.mark.parametrize('billed_card,booked_card', [('****1234', '****5678'),
                                                   ('****5678', '****1234')])
def test_cathay_dispatcher_owns_billed_and_booked_source_class(store, scope, billed_card, booked_card):
    blank = {**BASE, 'card_no': '', 'post_date': '2026-07-03T00:00:00'}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([blank, independent])
    before = edit_originals(store)
    payload = {'credit_card': {
        'billed_detail': {'TWD': [{**BASE, 'card_no': billed_card}, independent]},
        scope + '_detail': {'consume': [
            {**BASE, 'card_no': booked_card, 'booking_status': 'booked'},
            {**independent, 'desc': 'Independent new purchase', 'booking_status': 'booked'}]},
    }}
    persist_collected('cathay', payload, store)
    actual = rows(store)
    assert actual[:2] == before, 'Cathay dispatcher bypassed complete owning-batch evidence'
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('reverse', [False, True])
def test_split_exact_reservation_keeps_integral_original_currency_occurrences(store, reverse):
    fx = {**BASE, 'consume_currency': 'USD', 'consume_amount': 3}
    midnight = {**fx, 'post_date': '2026-07-03T00:00:00'}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([fx, independent])
    before = edit_originals(store)
    billed = [midnight, independent]
    booked = [{**fx, 'amount': '100.0', 'currency': 'twd'},
              {**independent, 'desc': 'Independent new purchase'}]
    if reverse:
        billed.reverse()
        booked.reverse()
    store.persist_card_batch(billed, [{**txn, 'booking_status': 'booked'} for txn in booked],
                             scope='current', fetch_ok=True)
    actual = rows(store)
    assert actual[:2] == before
    same = [row for row in actual if row['description'] == BASE['desc']]
    assert len(same) == 2, 'REAL affinity changed reservation money serialization and collapsed occurrences'
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('partition', ['billed-currencies', 'both-pending'])
def test_v4_collected_owner_sees_every_currency_and_pending_scope(store, reverse, partition):
    from copy import deepcopy
    blank = {**BASE, 'card_no': '', 'post_date': '2026-07-03T00:00:00'}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([blank, independent])
    before = edit_originals(store)
    crawler = CathayCrawler.__new__(CathayCrawler)
    raw = lambda card: {'cardNo': card, 'consumeDate': BASE['date'], 'postingDate': BASE['post_date'],
                        'transDesc': BASE['desc'], 'amount': '100.00'}
    a, b = [crawler._norm_card_txn(raw(card)) for card in ('9000000000001234', '9000000000005678')]
    # Currency container labels do not override per-row currency in the real producer.
    cc = {'billed_detail': {'TWD': [a, independent], 'USD': [b], 'CNY': []}}
    if partition == 'both-pending':
        cc = {'billed_detail': {'TWD': [independent]},
              'unbilled_detail': {'consume': [a]}, 'current_detail': {'consume': [b]}}
    cc.setdefault('current_detail', {'consume': []})['consume'].append(
        {**independent, 'desc': 'Independent new purchase', 'booking_status': 'booked'})
    if reverse:
        cc['billed_detail'] = dict(reversed(list(cc['billed_detail'].items())))
        for block in cc.values():
            for transactions in block.values():
                transactions.reverse()
    payload = {'credit_card': cc}
    source = deepcopy(payload)
    persist_collected('cathay', payload, store)
    actual = rows(store)
    assert actual[:2] == before, 'The collected-result owner must span all admitted partitions'
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)
    # The existing native classifier mutates txn_type only; observations must not alter producer money/dates.
    for name, block in cc.items():
        for key, transactions in block.items():
            assert [{k: v for k, v in t.items() if k != 'txn_type'} for t in transactions] == [
                {k: v for k, v in t.items() if k != 'txn_type'} for t in source['credit_card'][name][key]]


@pytest.mark.parametrize('scope', ['unbilled', 'current'])
@pytest.mark.parametrize('control', ['missing', 'failed', 'error', 'unrecognized', 'invalid-sibling',
                                    'placeholder', 'no-attestation', 'noncanonical', 'invalid-day'])
def test_v4_owner_unadmitted_scope_cannot_veto_unique_history(store, scope, control):
    blank = {**BASE, 'card_no': '', 'post_date': '2026-07-03T00:00:00'}
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([blank, independent])
    before = edit_originals(store)
    pending = {**BASE, 'card_no': '****5678', 'booking_status': 'booked'}
    envelope = {'consume': [pending]}
    if control == 'failed': envelope = None
    if control == 'error': envelope['errorMessage'] = 'synthetic failure'
    if control == 'unrecognized': envelope = {'metadata': [], 'unknown': 'not a successful consume response'}
    if control == 'invalid-sibling': envelope['consume'].append({**pending, 'desc': 'Invalid sibling', 'amount': 'NaN'})
    if control == 'placeholder': pending.update(amount=None, desc=None)
    if control == 'no-attestation': pending.pop('booking_status')
    if control == 'noncanonical': pending['post_date'] = '2026-07-03T00:00:00'
    if control == 'invalid-day': pending['post_date'] = '2026-02-30'
    cc = {'billed_detail': {'TWD': [BASE, independent, {**independent, 'desc': 'Independent new purchase'}]}}
    if control != 'missing': cc[scope + '_detail'] = envelope
    persist_collected('cathay', {'credit_card': cc}, store)
    assert rows(store)[:2] == [{**before[0], 'card_no': BASE['card_no']}, before[1]]
    assert any(row['description'] == 'Independent new purchase' for row in rows(store)[2:])
    assert all(row[field] in (None, 0) for row in rows(store)[2:] for field in OVERLAYS)


@pytest.mark.parametrize('scope', ['unbilled', 'current'])
@pytest.mark.parametrize('changed', [
    {'currency': 'USD'}, {'consume_currency': 'USD', 'consume_amount': 3},
    {'post_date': '2026-07-04'}, {'desc': '\tSynthetic format replay\t'},
    {'native_identity': 'different-reference'},
])
def test_v4_collected_owner_immutable_vetoes_match_eventual_writer(store, scope, changed):
    blank = {**BASE, 'card_no': '', 'post_date': '2026-07-03T00:00:00',
             **({'native_identity': 'history-reference'} if 'native_identity' in changed else {})}
    if blank.get('native_identity'):
        # Canonical native fixtures retain strict native dates; never bless a midnight native payload.
        blank.update(card_no=BASE['card_no'], post_date=BASE['post_date'])
    store.upsert_card_billed([blank])
    before = edit_originals(store)[0]
    pending = {**BASE, 'card_no': '****5678', 'booking_status': 'booked', **changed}
    persist_collected('cathay', {'credit_card': {
        'billed_detail': {'TWD': [{**BASE, **({'native_identity': 'history-reference'} if 'native_identity' in changed else {})}]},
        scope + '_detail': {'consume': [pending]}}}, store)
    assert rows(store)[0] == {**before, 'card_no': BASE['card_no']}


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('route', ['batch', 'collected'])
@pytest.mark.parametrize('currency,amount,original,original_currency', [
    ('TWD', 100, 3, 'USD'), ('twd', '100.00', '3.0', ' usd '),
    ('TWD', 100, 3.125, 'USD'), ('USD', '100.125', '-3.125', 'USD'),
    ('TWD', '-100', '-3', 'USD'), ('TWD', 100, '-0.0', 'USD'),
    ('TWD', '9007199254740990', '3', 'USD'), ('TWD', 100, None, None),
    ('TWD', 100, '3', None),
])
def test_v4_reservation_tokens_are_writer_money_not_sql_real(store, reverse, route, currency, amount, original, original_currency):
    from decimal import Decimal
    exact = {**BASE, 'currency': currency, 'amount': Decimal(amount) if isinstance(amount, str) else amount,
             'consume_currency': original_currency, 'consume_amount': original}
    if route == 'collected':
        from backend.core.classify import classify_by_desc_and_sign
        exact['txn_type'] = classify_by_desc_and_sign(exact['desc'], exact['amount'])
    independent = {**BASE, 'card_no': '****9999', 'desc': 'Independent exact purchase'}
    store.upsert_card_billed([exact, independent])
    before = edit_originals(store)
    billed = [{**exact, 'post_date': '2026-07-03T00:00:00'}, independent]
    booked = [{**exact, 'booking_status': 'booked'},
              {**independent, 'desc': 'Independent new purchase', 'booking_status': 'booked'}]
    if reverse: billed.reverse(); booked.reverse()
    if route == 'batch':
        store.persist_card_batch(billed, booked, scope='current', fetch_ok=True)
    else:
        persist_collected('cathay', {'credit_card': {'billed_detail': {'TWD': billed},
                           'current_detail': {'consume': booked}}}, store)
    actual = rows(store)
    assert actual[:2] == before
    assert len([r for r in actual if r['description'] == BASE['desc']]) == 2
    assert any(row['description'] == 'Independent new purchase' for row in actual[2:])
    assert all(row[field] in (None, 0) for row in actual[2:] for field in OVERLAYS)


@pytest.mark.parametrize('later_scope', ['USD', 'unbilled', 'current'])
@pytest.mark.parametrize('boundary', ['finite', 'classify'])
def test_v4_observations_do_not_execute_later_native_checkpoints(store, monkeypatch, later_scope, boundary):
    from backend.core.persist import cathay
    sentinel = RuntimeError('same escaping synthetic exception')
    original_upsert = store.upsert_card_billed
    original_finite = cathay._finite_amount
    original_classify = cathay.classify.classify_by_desc_and_sign
    events = []
    later = {**BASE, 'desc': 'Later native checkpoint', 'booking_status': 'booked'}
    def upsert(txns, *args, **kwargs):
        events.append('store:' + (txns[0]['desc'] if txns else 'empty'))
        return original_upsert(txns, *args, **kwargs)
    def finite(value, **kwargs):
        if kwargs.get('_codes', ('',) * 9)[8] in {'CATHAY-08-843-009', 'CATHAY-08-841-009', 'CATHAY-08-845-009'}:
            # Use the raw scalar identity to distinguish the later row without changing it.
            if value is later['amount'] and events:
                events.append('later-finite')
                if boundary == 'finite': raise sentinel
        return original_finite(value, **kwargs)
    def classify(desc, amount):
        if desc == later['desc']:
            events.append('later-classify')
            if boundary == 'classify': raise sentinel
        return original_classify(desc, amount)
    later['amount'] = 123456
    monkeypatch.setattr(store, 'upsert_card_billed', upsert)
    monkeypatch.setattr(cathay, '_finite_amount', finite)
    monkeypatch.setattr(cathay.classify, 'classify_by_desc_and_sign', classify)
    cc = {'billed_detail': {'TWD': [dict(BASE)]}}
    if later_scope == 'USD': cc['billed_detail']['USD'] = [later]
    else: cc[later_scope + '_detail'] = {'consume': [later]}
    with pytest.raises(RuntimeError) as caught:
        persist_collected('cathay', {'credit_card': cc}, store)
    assert caught.value is sentinel
    assert events[0] == 'store:' + BASE['desc'], 'Later native checkpoint was evaluated before the original first store write'


@pytest.mark.parametrize('field', ['amount', 'consume_amount'])
@pytest.mark.parametrize('later_value', [True, 'bad', 'NaN', 'sNaN', 'Infinity', '1e1000000',
                                       '9007199254740992', '0.0000001', '9007199254740990.125',
                                       '', '-', None])
def test_v4_later_money_siblings_keep_first_native_error_and_decimal_flags(store, field, later_value):
    from decimal import localcontext, Overflow, InvalidOperation
    from backend.core.error_codes import exception_error_id
    first = {**BASE, 'amount': '100.5'}
    later = {**BASE, 'card_no': '****5678', 'booking_status': 'booked',
             'consume_currency': 'USD', field: later_value}
    with localcontext() as context:
        context.Emax = 3
        context.traps[Overflow] = context.traps[InvalidOperation] = True
        context.clear_flags()
        traps = dict(context.traps)
        with pytest.raises(ValueError) as caught:
            persist_collected('cathay', {'credit_card': {'billed_detail': {'TWD': [first]},
                               'current_detail': {'consume': [later]}}}, store)
        assert exception_error_id(caught.value) == 'CATHAY-08-865-007'
        assert caught.value.args == ('invalid native monetary value',)
        assert not caught.value.__suppress_context__
        assert dict(context.traps) == traps
        assert not any(context.flags.values()), 'Comparison observations leaked later-money flags'
    assert rows(store) == []
