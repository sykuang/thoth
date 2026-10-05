"""Export actual writer/PATCH/replica snapshots for the production JS projector."""
import json
import os
from pathlib import Path
from backend.core.store import BankStore
from tests.test_loan_timeline import seed


def test_user_edits_backend_frontend_contract(client, monkeypatch, tmp_path):
    headers, account, rows = seed(client, monkeypatch, tmp_path)
    snapshots = []

    def capture(label):
        response = client.get('/replica/bootstrap', headers=headers)
        assert response.status_code == 200
        facts = next(p['data'] for p in response.json()['partitions'] if p['name'] == 'bank:sinopac')
        items = client.get('/transactions?include_loan_repayments=true', headers=headers).json()['items']
        stats = client.get('/transactions/stats?include_loan_repayments=true', headers=headers).json()
        assert stats['total_income'] == '0'
        snapshots.append((label, {'facts': facts, 'items': items, 'stats': stats}))
        return items

    items = capture('original')
    urls = {item['component']: '/transactions/sinopac/loan_repayment/' + item['id'] for item in items}
    response = client.patch(urls['interest'], headers=headers,
                            json={'category': '房貸', 'subcategory': None, 'auto_excluded': True})
    assert response.status_code == 200
    capture('interest-excluded')
    assert snapshots[-1][1]['stats']['total_expense'] == '0.025'
    response = client.patch(urls['principal'], headers=headers, json={'category': '收入', 'auto_excluded': True})
    assert response.status_code == 200
    capture('principal-excluded')
    response = client.patch(urls['interest'], headers=headers,
                            json={'category': '🏠' * 100, 'subcategory': '房貸', 'auto_excluded': False})
    assert response.status_code == 200
    capture('unicode-included')
    assert snapshots[-1][1]['stats']['total_expense'] == '0.15'
    assert client.patch(urls['interest'], headers=headers, json={'category': '🏠' * 101}).status_code == 400
    writer = BankStore('sinopac', user_id=1, source_account_id=account['id'])
    try:
        for payload in ([], rows):
            writer.replace_loan_repayments('LOAN-1', '01', 'TWD', '2026-06-01', '2026-06-30', payload)
            writer.commit()
    finally:
        writer.close()
    capture('after-sync')
    assert snapshots[-1][1] == snapshots[-2][1]
    response = client.patch(urls['interest'], headers=headers, json={'category': None, 'subcategory': None})
    assert response.status_code == 200
    capture('cleared-category')
    if target := os.environ.get('THOTH_LOAN_EDIT_FIXTURE_DIR'):
        output = Path(target)
        output.mkdir(parents=True, exist_ok=True)
        for label, snapshot in snapshots:
            (output / (label + '.json')).write_text(json.dumps(snapshot, ensure_ascii=False))
