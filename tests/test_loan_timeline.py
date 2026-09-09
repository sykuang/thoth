"""Loan facts travel through the real writer, tenant read, API and replica."""
import pytest
from backend.core.store import BankStore
from tests.test_transactions_routes import _register, _auth


def seed(client, monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_ROOT', str(tmp_path))
    from backend.core import store
    monkeypatch.setattr(store, 'DATA_ROOT', tmp_path)
    token = _register(client)
    headers = _auth(token)
    account = client.post('/accounts', json={'bank': 'sinopac', 'label': 'loan'}, headers=headers).json()
    rows = [dict(due_date='2026-06-01', paid_on='2026-06-02', principal='9007199254740993.25', interest='0.125', penalty='0.025', paid_total='9007199254740993.4', principal_balance='100.10', status='paid', raw_json='{"synthetic":1}')]
    writer = BankStore('sinopac', user_id=1, source_account_id=account['id'])
    try:
        writer.replace_loan_repayments('LOAN-1', '01', 'TWD', '2026-06-01', '2026-06-30', rows)
        writer.commit()
    finally:
        writer.close()
    return headers, account, rows


def test_writer_to_route_detail_stats_replica(client, monkeypatch, tmp_path):
    headers, account, rows = seed(client, monkeypatch, tmp_path)
    assert client.get('/transactions', headers=headers).json()['total'] == 0
    result = client.get('/transactions?include_loan_repayments=true', headers=headers)
    assert result.status_code == 200, result.text
    items = result.json()['items']
    assert len(items) == 3
    by_component = {item['component']: item for item in items}
    assert by_component['principal']['amount'] == '9007199254740993.25'
    assert by_component['principal']['cashflow_direction'] == 'neutral'
    assert by_component['interest']['amount'] == '-0.125'
    assert by_component['penalty']['amount'] == '-0.025'
    for item in items:
        assert item['read_only'] is True
        assert item['reconciliation_status'] == 'unverified'
        detail = client.get('/transactions/sinopac/loan_repayment/' + item['id'], headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json() == item
    stats = client.get('/transactions/stats?include_loan_repayments=true', headers=headers).json()
    assert stats['total_income'] == '0'
    assert stats['total_expense'] == '0.15'
    assert client.get('/transactions/stats', headers=headers).json()['total_expense'] == 0
    from backend.server.db_facade import db_api
    facts = db_api.list_loan_repayments(bank='sinopac', user_id=1)
    assert len(facts) == 1
    from backend.server.replica_facts import collect_bank_replica_facts
    replica = collect_bank_replica_facts('sinopac', 1)
    assert replica['loan_repayments'] == [items[0]['loan_repayment']]
    assert replica['transactions'] == []
    assert 'raw_json' not in replica['loan_repayments'][0]
    response = client.get('/replica/bootstrap', headers=headers)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('body', [{}, {'category': 'x'}, {'splits': []}, {'auto_excluded': True}])
def test_read_only_patch(client, monkeypatch, tmp_path, body):
    headers, account, rows = seed(client, monkeypatch, tmp_path)
    item = client.get('/transactions?kind=loan_repayment', headers=headers).json()['items'][0]
    result = client.patch('/transactions/sinopac/loan_repayment/' + item['id'], json=body, headers=headers)
    assert result.status_code == 400
    assert '僅供讀取' in result.json()['detail']


def test_bulk_guard_precedes_writes(client, monkeypatch, tmp_path):
    seed(client, monkeypatch, tmp_path)
    from backend.server.db_facade import db_api
    with db_api.transaction(bank='sinopac') as tx:
        with pytest.raises(ValueError, match='read-only'):
            tx.batch_update_categorization(user_id=1, updates=[{'table': 'loan_repayments', 'id': 'opaque'}])
        with pytest.raises(ValueError):
            tx.update_txn(kind='loan_repayment', txn_id='opaque', user_id=1)


def test_exact_large_fee_and_currency_separation(client, monkeypatch, tmp_path):
    headers, account, rows = seed(client, monkeypatch, tmp_path)
    rows[0]['interest'] = '123456789012345678901234567890.123456789'
    writer = BankStore('sinopac', user_id=1, source_account_id=account['id'])
    writer.replace_loan_repayments('LOAN-1', '01', 'TWD', '2026-06-01', '2026-06-30', rows)
    writer.commit()
    stats = client.get('/transactions/stats?kind=loan_repayment', headers=headers).json()
    assert stats['total_expense'] == '123456789012345678901234567890.148456789'
    writer.replace_loan_repayments('FX', '01', 'USD', '2026-06-01', '2026-06-30', rows)
    writer.commit()
    writer.close()
    from backend.server.dashboard_cache import clear_dashboard_cache
    clear_dashboard_cache()
    mixed = client.get('/transactions/stats?kind=loan_repayment', headers=headers)
    assert mixed.status_code == 400
    usd = client.get('/transactions/stats?kind=loan_repayment&currency=USD', headers=headers)
    assert usd.status_code == 200
    assert usd.json()['total'] == 3


def test_tenant_source_occurrence_snapshots_and_missing_table(client, monkeypatch, tmp_path):
    headers, account, rows = seed(client, monkeypatch, tmp_path)
    from backend.server.db_facade import db_api
    first = db_api.list_loan_repayments(bank='sinopac', user_id=1)
    assert db_api.get_loan_repayment(bank='sinopac', user_id=1, fact_id=first[0].id) == first[0]
    assert db_api.get_loan_repayment(bank='sinopac', user_id=1, fact_id='bad') is None
    other_headers = _auth(_register(client, email='other@palace.example'))
    other = client.post('/accounts', json={'bank': 'sinopac', 'label': 'other'}, headers=other_headers).json()
    second = client.post('/accounts', json={'bank': 'sinopac', 'label': 'second'}, headers=headers).json()
    def write(user, source, data, start='2026-06-01'):
        writer = BankStore('sinopac', user_id=user, source_account_id=source)
        writer.replace_loan_repayments('LOAN-1', '01', 'TWD', start, '2026-06-30', data)
        writer.commit()
        writer.close()
    write(1, other['id'], rows)  # Corrupt ownership must not become visible.
    write(2, other['id'], rows)
    assert db_api.list_loan_repayments(bank='sinopac', user_id=1) == first
    assert db_api.list_loan_repayments(bank='sinopac', user_id=1, source_account_id=other['id']) == []
    foreign_id = first[0].id + ':principal'
    assert client.get('/transactions/sinopac/loan_repayment/' + foreign_id, headers=other_headers).status_code == 404
    assert client.get('/transactions/sinopac/loan_repayment/bad', headers=headers).status_code == 404
    before = client.get('/replica/bootstrap', headers=headers).json()
    write(1, account['id'], rows)
    repeated = client.get('/replica/bootstrap', headers=headers).json()
    assert before['generations'] == repeated['generations']
    write(1, account['id'], rows * 2)
    write(1, second['id'], rows)
    facts = db_api.list_loan_repayments(bank='sinopac', user_id=1)
    assert len({fact.id for fact in facts}) == 3
    scoped = client.get(f'/transactions?kind=loan_repayment&account_id={second["id"]}', headers=headers).json()
    assert scoped['total'] == 3
    scoped_stats = client.get(f'/transactions/stats?kind=loan_repayment&account_id={second["id"]}', headers=headers).json()
    assert scoped_stats['total'] == 3
    assert len(db_api.list_loan_repayments(bank='sinopac', user_id=1, source_account_id=account['id'])) == 2
    after = client.get('/replica/bootstrap', headers=headers).json()
    assert after['generations']['bank:sinopac'] > repeated['generations']['bank:sinopac']
    partition = next(p['data'] for p in after['partitions'] if p['name'] == 'bank:sinopac')
    assert partition['loan_repayments'] == [fact.model_dump() for fact in facts]
    write(1, account['id'], [])
    assert len(db_api.list_loan_repayments(bank='sinopac', user_id=1)) == 1
    import sqlite3
    con = sqlite3.connect(tmp_path / 'sinopac.sqlite')
    con.execute('DROP TABLE loan_repayments')
    con.commit()
    assert db_api.list_loan_repayments(bank='sinopac', user_id=1) == []
    assert con.execute("SELECT name FROM sqlite_master WHERE name='loan_repayments'").fetchall() == []
    con.execute('CREATE TABLE loan_repayments (user_id INTEGER)')
    con.commit()
    with pytest.raises(sqlite3.OperationalError):
        db_api.list_loan_repayments(bank='sinopac', user_id=1)
    con.close()


def test_zero_components_and_query_stats_parity(client, monkeypatch, tmp_path):
    headers, account, rows = seed(client, monkeypatch, tmp_path)
    from backend.server.routers.transactions import _compute_transactions_stats
    kwargs = dict(banks=['sinopac'], kinds=['loan_repayment'], since=None, until=None,
                  category=None, card_date_basis='consume', user_id=1)
    assert _compute_transactions_stats(q=None, **kwargs)['total_expense'] == '0.15'
    assert _compute_transactions_stats(q='貸款', **kwargs)['total_expense'] == '0.15'
    assert client.get('/transactions?kind=loan_repayment&direction=income', headers=headers).json()['items'] == []
    writer = BankStore('sinopac', user_id=1, source_account_id=account['id'])
    rows[0].update(principal='0.00', interest='0', penalty='0.000')
    writer.replace_loan_repayments('LOAN-1', '01', 'TWD', '2026-06-01', '2026-06-30', rows)
    writer.commit()
    writer.close()
    assert client.get('/transactions?kind=loan_repayment', headers=headers).json()['items'] == []
    from backend.server.db_facade import db_api
    assert len(db_api.list_loan_repayments(bank='sinopac', user_id=1)) == 1
