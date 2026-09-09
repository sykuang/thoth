"""Integration guards for loan readers and existing numeric transaction routes."""
import json
import os
from pathlib import Path
import pytest
from backend.core.store import BankStore
from tests.test_loan_timeline import seed


@pytest.mark.parametrize('method', ['get', 'patch'])
def test_legacy_transaction_routes_keep_numeric_database_ids(client, monkeypatch, tmp_path, method):
    headers, _, _ = seed(client, monkeypatch, tmp_path)
    from backend.server.db_facade import db_api
    seen = []
    def get_txn(**kwargs):
        seen.append(kwargs['txn_id'])
        return None
    monkeypatch.setattr(db_api, 'get_txn', get_txn)
    if method == 'get':
        response = client.get('/transactions/sinopac/twd/7', headers=headers)
    else:
        response = client.patch('/transactions/sinopac/twd/7', headers=headers,
                                json={'splits': [{'amount': 1}, {'amount': 2}]})
    assert response.status_code == 404
    assert seen == [7]
    assert type(seen[0]) is int


def test_negative_loan_fact_is_not_silently_reversed(client, monkeypatch, tmp_path):
    _, account, rows = seed(client, monkeypatch, tmp_path)
    rows[0]['interest'] = '-0.125'
    writer = BankStore('sinopac', user_id=1, source_account_id=account['id'])
    try:
        writer.replace_loan_repayments('LOAN-1', '01', 'TWD', '2026-06-01', '2026-06-30', rows)
        writer.commit()
    finally:
        writer.close()
    from backend.server.db_facade import db_api
    with pytest.raises(ValueError, match='negative loan amount'):
        db_api.list_loan_repayments(bank='sinopac', user_id=1)


def test_real_backend_contract_for_frontend(client, monkeypatch, tmp_path):
    headers, _, _ = seed(client, monkeypatch, tmp_path)
    response = client.get('/replica/bootstrap', headers=headers)
    assert response.status_code == 200
    data = next(p['data'] for p in response.json()['partitions'] if p['name'] == 'bank:sinopac')
    items = client.get('/transactions?include_loan_repayments=true', headers=headers).json()['items']
    stats = client.get('/transactions/stats?include_loan_repayments=true', headers=headers).json()
    assert stats['total_income'] == '0' and stats['total_expense'] == '0.15'
    if path := os.environ.get('THOTH_LOAN_TEST_FIXTURE'):
        Path(path).write_text(json.dumps({'facts': data, 'items': items, 'stats': stats}))
