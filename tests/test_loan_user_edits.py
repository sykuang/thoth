"""Synthetic real bank writer → sparse user metadata → all read paths."""
import pytest
from backend.core.store import BankStore
from tests.test_loan_timeline import seed


def test_patch_persists_across_writer_and_replica(client, monkeypatch, tmp_path):
    headers, account, rows = seed(client, monkeypatch, tmp_path)
    items = client.get('/transactions?kind=loan_repayment', headers=headers).json()['items']
    interest = next(i for i in items if i['component'] == 'interest')
    url = '/transactions/sinopac/loan_repayment/' + interest['id']
    before = client.get('/replica/bootstrap', headers=headers).json()
    result = client.patch(url, headers=headers, json={'category': '自訂', 'auto_excluded': True})
    assert result.status_code == 200, result.text
    assert result.json()['category'] == '自訂'
    assert result.json()['read_only'] is False
    assert result.json()['amount'] == '-0.125'
    assert client.get('/transactions/stats?kind=loan_repayment', headers=headers).json()['total_expense'] == '0.025'
    after = client.get('/replica/bootstrap', headers=headers).json()
    assert after['generations']['bank:sinopac'] > before['generations']['bank:sinopac']
    fact = next(p['data'] for p in after['partitions'] if p['name'] == 'bank:sinopac')['loan_repayments'][0]
    assert fact['component_overrides'] == {'interest': {'category': '自訂', 'auto_excluded': True}}
    writer = BankStore('sinopac', user_id=1, source_account_id=account['id'])
    for data in ([], rows):
        writer.replace_loan_repayments('LOAN-1', '01', 'TWD', '2026-06-01', '2026-06-30', data)
        writer.commit()
        if not data:
            assert client.get(url, headers=headers).status_code == 404
            assert client.patch(url, headers=headers, json={'category': 'missing'}).status_code == 404
    writer.close()
    assert client.get(url, headers=headers).json()['category'] == '自訂'
    cleared = client.patch(url, headers=headers, json={'category': None, 'auto_excluded': False})
    assert cleared.json()['category'] is None
    assert cleared.json()['subcategory'] == '貸款利息'
    assert client.get('/transactions/stats?kind=loan_repayment', headers=headers).json()['total_expense'] == '0.15'
    principal = next(i for i in items if i['component'] == 'principal')
    result = client.patch('/transactions/sinopac/loan_repayment/' + principal['id'], headers=headers,
                          json={'category': '薪資', 'auto_excluded': False})
    assert result.json()['cashflow_direction'] == 'neutral'
    assert client.get('/transactions/stats?kind=loan_repayment', headers=headers).json()['total_income'] == '0'


@pytest.mark.parametrize('body', [{}, {'category': 'x', 'amount': 9}, {'auto_excluded': 1},
    {'auto_excluded': None}, {'category': []}, {'subcategory': 1}, {'category': 'x' * 101},
    {'__proto__': {}}, {'splits': []}])
def test_invalid_patch_creates_no_sidecar(client, monkeypatch, tmp_path, body):
    headers, _, _ = seed(client, monkeypatch, tmp_path)
    item = client.get('/transactions?kind=loan_repayment', headers=headers).json()['items'][0]
    result = client.patch('/transactions/sinopac/loan_repayment/' + item['id'], json=body, headers=headers)
    assert result.status_code == 400
    from backend.server import db
    from backend.core import bank_data
    con = db.open_bank_conn('sinopac')
    assert 'loan_transaction_overrides' not in bank_data.table_names(con)
    assert con is not None
    con.close()


def test_concurrent_partial_merge_and_isolation(client, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from tests.test_transactions_routes import _register, _auth
    from backend.server.db_facade import db_api
    headers, account, rows = seed(client, monkeypatch, tmp_path)
    second = client.post('/accounts', json={'bank': 'sinopac', 'label': 'second'}, headers=headers).json()
    writer = BankStore('sinopac', user_id=1, source_account_id=second['id'])
    writer.replace_loan_repayments('LOAN-1', '01', 'TWD', '2026-06-01', '2026-06-30', rows)
    writer.commit()
    writer.close()
    fact = db_api.list_loan_repayments(bank='sinopac', user_id=1, source_account_id=account['id'])[0]
    txn_id = fact.id + ':interest'
    barrier = Barrier(2)
    def update(changes):
        barrier.wait()
        return db_api.update_loan_transaction(bank='sinopac', user_id=1, txn_id=txn_id, changes=changes)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(update, [{'category': 'concurrent'}, {'auto_excluded': True}]))
    fresh = db_api.get_loan_repayment(bank='sinopac', user_id=1, fact_id=fact.id)
    assert fresh.component_overrides == {'interest': {'category': 'concurrent', 'auto_excluded': True}}
    assert db_api.list_loan_repayments(bank='sinopac', user_id=1, source_account_id=second['id'])[0].component_overrides == {}
    other = _auth(_register(client, email='other@palace.example'))
    client.post('/accounts', json={'bank': 'sinopac', 'label': 'other'}, headers=other)
    url = '/transactions/sinopac/loan_repayment/' + txn_id
    assert client.patch(url, headers=other, json={'category': 'stolen'}).status_code == 404
    assert client.get(url, headers=other).status_code == 404
    assert client.patch(url, headers=headers, json={'category': 'partial', 'auto_excluded': 'bad'}).status_code == 400
    assert client.get(url, headers=headers).json()['category'] == 'concurrent'


def test_cache_clear_old_schema_and_malformed_stored_metadata(client, monkeypatch, tmp_path):
    from backend.server import db
    from backend.core import bank_data
    from backend.server.db_facade import db_api
    from backend.server.dashboard_cache import get_or_set_dashboard_cache, dashboard_cache_size, clear_dashboard_cache
    headers, _, _ = seed(client, monkeypatch, tmp_path)
    fact = db_api.list_loan_repayments(bank='sinopac', user_id=1)[0]
    assert fact.component_overrides == {}
    con = db.open_bank_conn('sinopac')
    assert con is not None
    assert 'loan_transaction_overrides' not in bank_data.table_names(con)
    con.close()
    clear_dashboard_cache()
    get_or_set_dashboard_cache(namespace='test.loan-edits', user_id=1, params=('x',), compute=lambda: 5)
    assert dashboard_cache_size() == 1
    url = '/transactions/sinopac/loan_repayment/' + fact.id + ':interest'
    result = client.patch(url, headers=headers, json={'category': ''})
    assert result.status_code == 200
    assert result.json()['category'] is None
    assert dashboard_cache_size() == 0
    con = db.open_bank_conn('sinopac')
    assert con is not None
    con.execute('UPDATE loan_transaction_overrides SET override_json=?', ('{"auto_excluded": "false"}',))
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        db_api.list_loan_repayments(bank='sinopac', user_id=1)
    assert client.patch(url, headers=headers, json={'category': 'repair'}).status_code == 400
