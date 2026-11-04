"""Cathay's own C0102/C0108 labels distinguish 入帳起息日 from 折算日."""
import pytest

from backend.banks.cathay import CathayCrawler
from backend.core.base import ApiHit, ResponseCollector
from backend.core.persist import persist_collected
from backend.core.store import BankStore


@pytest.mark.parametrize('override,clears', [
    ({}, True),
    ({'returnCode': 'U702'}, True),  # Synthetic code, official NoData suffix.
    ({'returnCode': 'U912'}, False),
    ({'returnCode': 'W503'}, False),
    ({'success': False}, False),
    ({'success': 1}, False),
    ({'success': 'true'}, False),
    ({'missing_content': True}, False),
    ({'status': 403}, False),
    ({'returnCode': None}, False),
    ({'returnCode': ''}, False),
    ({'returnCode': 702}, False),
    ({'content': {}}, False),
    ({'content': []}, False),
    ({'content': 'malformed'}, False),
    ({'error': 'synthetic'}, False),
    ({'errors': ['synthetic']}, False),
    ({'errorCode': 'synthetic'}, False),
    ({'errorMessage': 'synthetic'}, False),
    ({'missing_endpoint': True}, False),
    ({'endpoint': 'C_BILL_Q_CardUnbilledConsume'}, False),
])
def test_cathay_attested_empty_current_replaces_only_current(
    override, clears, tmp_path, monkeypatch,
):
    monkeypatch.setenv('BANK_DATA_ROOT', str(tmp_path))
    body = {'success': True, 'returnCode': '0000', 'content': None, **override}
    status = body.pop('status', 200)
    endpoint = body.pop('endpoint', 'C_BILL_Q_CardCurrentConsume')
    missing = body.pop('missing_endpoint', False)
    if body.pop('missing_content', False):
        del body['content']
    collector = ResponseCollector()
    if not missing:
        collector.hits.append(ApiHit(url=f'https://fixture.invalid/{endpoint}', method='POST',
                                     status=status, resp_json=body))
    parsed = CathayCrawler.__new__(CathayCrawler)._parse(collector)
    store = BankStore('cathay', user_id=7, source_account_id=91)
    try:
        for scope in ('current', 'unbilled'):
            store.refresh_card_pending(scope, [{
                'card_no': '****1234', 'date': '2026-10-24', 'desc': 'Synthetic old row',
                'amount': 1824, 'currency': 'TWD',
            }], rules=[], fetch_ok=True)
        persist_collected('cathay', parsed, store)
        rows = store.conn.execute('SELECT scope FROM card_pending_txns ORDER BY scope').fetchall()
        assert [r['scope'] for r in rows] == (['unbilled'] if clears else ['current', 'unbilled'])
    finally:
        store.close()


@pytest.mark.parametrize('occurrences', [1, 2])
@pytest.mark.parametrize('field,parser,container', [
    ('beginValueDate', '_parse_bill_detail', 'twdBillDetailInfo'),
    ('valueDateTime', '_parse_consume', 'twdUnbilledConsumeDetail'),
])
def test_cathay_native_booking_date_survives_parser_and_replay(
    field, parser, container, occurrences, tmp_path, monkeypatch,
):
    monkeypatch.setenv('BANK_DATA_ROOT', str(tmp_path))
    crawler = CathayCrawler.__new__(CathayCrawler)
    raw = {'cardNo': '9000000000001234', 'consumeDate': '2026-10-03T00:00:00',
           'transDesc': 'Synthetic merchant', 'amount': 983, 'currency': 'TWD',
           field: '2026-10-06T00:00:00', 'convertDate': '2026-10-07'}
    source_rows = [dict(raw) for _ in range(occurrences)]
    payload = ({container: [{'tradeData': source_rows}]} if parser == '_parse_bill_detail'
               else {container: source_rows})
    parsed = getattr(crawler, parser)(payload)
    rows = next(iter(parsed.values()))
    assert len(rows) == occurrences
    assert all(r['post_date'] == '2026-10-06' and r['booking_status'] == 'booked' for r in rows)
    detail = 'billed_detail' if parser == '_parse_bill_detail' else 'unbilled_detail'
    store = BankStore('cathay', user_id=7, source_account_id=91)
    try:
        data = {'credit_card': {detail: parsed}}
        persist_collected('cathay', data, store)
        ids = [r['id'] for r in store.conn.execute('SELECT id FROM card_billed_txns ORDER BY id')]
        store.conn.execute("UPDATE card_billed_txns SET category='Synthetic custom' WHERE id=?", (ids[0],))
        store.conn.commit()
        persist_collected('cathay', data, store)
        rows = store.conn.execute('SELECT id,post_date,category FROM card_billed_txns ORDER BY id').fetchall()
        assert [r['id'] for r in rows] == ids
        assert [r['post_date'] for r in rows] == ['2026-10-06'] * occurrences
        assert rows[0]['category'] == 'Synthetic custom'
        assert store.conn.execute('SELECT COUNT(*) AS n FROM card_pending_txns').fetchone()['n'] == 0
    finally:
        store.close()
