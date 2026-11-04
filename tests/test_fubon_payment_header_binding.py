"""Unknown Fubon column order must not manufacture a remaining balance."""
import json
from pathlib import Path

import pytest

from backend.banks.fubon import _fubon_card_bill_fact
from backend.core.persist.fubon import _parse_fubon_credit_card
from backend.core.card_bills import publish_card_bill_facts
from backend.core.persist import persist_collected
from backend.core.store import BankStore


@pytest.mark.parametrize('remaining', [0, 400])
@pytest.mark.parametrize('header', [
    '繳款狀態\t最近繳款日期\t已繳金額\t本期帳單剩餘應繳金額\t自動扣繳帳號',
    '繳款狀態\t最近繳款日\t繳款金額\t剩餘應繳\t自動扣繳帳號',
])
def test_fubon_valid_remaining_reaches_all_known_cards(header, remaining, tmp_path, monkeypatch):
    monkeypatch.setenv('BANK_DATA_ROOT', str(tmp_path))
    text = ('本期帳單結帳日\t應繳總金額\t最低應繳金額\t繳款截止日\t本期循環利率\n'
            '2026/10/16\t800\t80\t2026/11/01\t12.62%\n'
            + header + f'\n未繳款\t2026/11/03\t400\t{remaining}\tSynthetic masked account\n')
    from tests.test_fubon_history_sync import _payload
    data = {**_payload(), 'amount_page_text': text}
    fixture = json.loads((Path(__file__).parent / 'fixtures/fubon_collected_fake.json').read_text())
    data['cards_page_text'] = fixture['data']['cards_page_text']
    current = _parse_fubon_credit_card(data)['cards'][0]['number']
    omitted = '****4444'
    assert omitted not in [c['number'] for c in _parse_fubon_credit_card(data)['cards']]
    publish_card_bill_facts(data, [_fubon_card_bill_fact(text)])
    assert data['card_bill_facts_ok'] is True
    store = BankStore('fubon', user_id=7, source_account_id=91)
    try:
        store.upsert_cards([{'number': tail, 'name': 'Synthetic card', 'bill_due_amount': 800,
                             'statement_close_date': '2026-10-16', 'payment_due_date': '2026-11-01'}
                            for tail in (current, omitted)])
        persist_collected('fubon', data, store)
        rows = store.conn.execute('SELECT bill_due_amount,last_payment_amount,last_payment_date FROM cards').fetchall()
        assert len(rows) == 4
        assert all(tuple(row) == (remaining, 400, '2026-11-03') for row in rows)
    finally:
        store.close()


@pytest.mark.parametrize('header', [
    '繳款狀態\t最近繳款日期\t本期帳單剩餘應繳金額\t已繳金額\t自動扣繳帳號',
    '繳款狀態\t最近繳款日期\t已繳金額\t其他金額\t自動扣繳帳號',
    '繳款狀態\t最近繳款日期\t已繳金額\t自動扣繳帳號',
])
@pytest.mark.parametrize('consumer', ['canonical', 'legacy'])
def test_fubon_unknown_payment_columns_do_not_publish_debt(header, consumer):
    text = ('本期帳單結帳日\t應繳總金額\t最低應繳金額\t繳款截止日\t本期循環利率\n'
            '2026/10/16\t800\t80\t2026/11/01\t12.62%\n'
            + header + '\n未繳款\t2026/11/03\t0\t800\tSynthetic masked account\n')
    if consumer == 'canonical':
        assert _fubon_card_bill_fact(text) is None
    else:
        summary = _parse_fubon_credit_card({'amount_page_text': text})['billing_summary']
        assert 'remaining_due' not in summary
        assert 'paid_amount' not in summary


@pytest.mark.parametrize('row', [
    '未繳款\t2026/11/03\t400\t\t800\tSynthetic',
    '未繳款\t2026/11/03\t400\t\t0\tSynthetic',
    '未繳款\t2026/11/03\t400\n\t800\tSynthetic',
    '未繳款\t2026/11/03\t\t400\t800\tSynthetic',
])
@pytest.mark.parametrize('consumer', ['canonical', 'legacy'])
def test_fubon_empty_columns_cannot_borrow_another_cell_or_row(row, consumer):
    text = ('繳款狀態\t最近繳款日期\t已繳金額\t本期帳單剩餘應繳金額\t自動扣繳帳號\n'
            + row + '\n')
    if consumer == 'canonical':
        assert _fubon_card_bill_fact(text) is None
    else:
        summary = _parse_fubon_credit_card({'amount_page_text': text})['billing_summary']
        assert 'remaining_due' not in summary
        assert 'paid_amount' not in summary


@pytest.mark.parametrize('row', [
    '2026/10/16\t800\t\t80\t2026/11/01',
    '2026/10/16\t800\t80\n\t2026/11/01',
])
@pytest.mark.parametrize('consumer', ['canonical', 'legacy'])
def test_fubon_statement_cannot_borrow_empty_columns_or_next_row(row, consumer):
    text = ('本期帳單結帳日\t應繳總金額\t最低應繳金額\t繳款截止日\t本期循環利率\n'
            + row + '\n'
            '繳款狀態\t最近繳款日期\t已繳金額\t本期帳單剩餘應繳金額\t自動扣繳帳號\n'
            '未繳款\t2026/11/03\t400\t400\tSynthetic\n')
    if consumer == 'canonical':
        fact = _fubon_card_bill_fact(text)
        assert fact is None or ('statement_close_date' not in fact and 'payment_due_date' not in fact)
    else:
        summary = _parse_fubon_credit_card({'amount_page_text': text})['billing_summary']
        assert 'bill_date' not in summary
        assert 'payment_due' not in summary


@pytest.mark.parametrize('borrowed', [0, 800])
def test_fubon_unavailable_payment_preserves_saved_bill_tuple(borrowed, tmp_path, monkeypatch):
    from tests.test_fubon_history_sync import _payload
    monkeypatch.setenv('BANK_DATA_ROOT', str(tmp_path))
    text = ('本期帳單結帳日\t應繳總金額\t最低應繳金額\t繳款截止日\t本期循環利率\n'
            '2026/10/16\t800\t80\t2026/11/01\t12.62%\n'
            '繳款狀態\t最近繳款日期\t已繳金額\t本期帳單剩餘應繳金額\t自動扣繳帳號\n'
            f'未繳款\t2026/11/03\t400\t\t{borrowed}\tSynthetic\n')
    data = {**_payload(), 'amount_page_text': text}
    fixture = json.loads((Path(__file__).parent / 'fixtures/fubon_collected_fake.json').read_text())
    data['cards_page_text'] = fixture['data']['cards_page_text']
    current = _parse_fubon_credit_card(data)['cards'][0]['number']
    omitted = '****4444'
    assert omitted not in [c['number'] for c in _parse_fubon_credit_card(data)['cards']]
    publish_card_bill_facts(data, [_fubon_card_bill_fact(text)])
    assert data['card_bill_facts_ok'] is False
    assert data['card_bill_facts'] == []
    store = BankStore('fubon', user_id=7, source_account_id=91)
    try:
        store.upsert_cards([{'number': tail, 'name': 'Synthetic card',
                             'bill_due_amount': due, 'statement_close_date': '2026-10-16',
                             'payment_due_date': '2026-11-01', 'last_payment_amount': payment,
                             'last_payment_date': '2026-11-02'}
                            for tail, due, payment in [(current, 177, 44), (omitted, 188, 55)]])
        query = ('SELECT card_no,bill_due_amount,statement_close_date,payment_due_date,'
                 'last_payment_amount,last_payment_date FROM cards '
                 'WHERE card_no IN (?,?) ORDER BY card_no')
        before = [tuple(row) for row in store.conn.execute(query, (current, omitted))]
        assert len(before) == 2
        persist_collected('fubon', data, store)
        assert [tuple(row) for row in store.conn.execute(query, (current, omitted))] == before
        rows = store.conn.execute('SELECT bill_due_amount FROM cards WHERE card_no NOT IN (?,?)',
                                  (current, omitted)).fetchall()
        assert len(rows) == 2 and all(row['bill_due_amount'] is None for row in rows)
    finally:
        store.close()

