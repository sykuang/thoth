"""Credential-owned native loan observations and durable component user edits."""
from decimal import Decimal
from hashlib import sha256
import json
import re
from pydantic import BaseModel, Field, field_validator
from backend.core import bank_data
from backend.server import db
from backend.server.creds_store import AccountsRepo


COMPONENTS = {'principal', 'interest', 'penalty'}


def validate_loan_edit(value: dict, *, require_fields: bool = False) -> dict:
    if not isinstance(value, dict) or set(value) - {'category', 'subcategory', 'auto_excluded'}:
        raise ValueError('不支援編輯的貸款欄位')
    if require_fields and not value:
        raise ValueError('請至少提供一個欄位')
    result = dict(value)
    for key, item in value.items():
        if key == 'auto_excluded':
            if type(item) is not bool:
                raise ValueError('auto_excluded 只接受 true/false')
        elif item is not None:
            if not isinstance(item, str) or len(item) > 100:
                raise ValueError(f'{key} 必須為 100 字內字串或 null')
            result[key] = item or None
    return result


def decimal_string(value) -> str:
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError('non-finite loan amount')
    text = format(value, 'f')
    return (text.rstrip('0').rstrip('.') if '.' in text else text) if value else '0'


class LoanRepaymentFact(BaseModel):
    id: str
    bank: str
    source_account_id: int
    account_no: str
    sub_account: str
    currency: str
    due_date: str
    paid_on: str
    status: str
    query_start: str
    query_end: str
    principal: str
    interest: str
    penalty: str
    paid_total: str
    principal_balance: str
    component_overrides: dict[str, dict] = Field(default_factory=dict)

    @field_validator('component_overrides', mode='before')
    @classmethod
    def validate_overrides(cls, value):
        if not isinstance(value, dict) or set(value) - COMPONENTS:
            raise ValueError('invalid loan component overrides')
        return {key: validate_loan_edit(item) for key, item in value.items()}

    @field_validator('principal', 'interest', 'penalty', 'paid_total', 'principal_balance')
    @classmethod
    def validate_amount(cls, value: str) -> str:
        normalized = decimal_string(value)
        if Decimal(normalized) < 0:
            raise ValueError('negative loan amount')
        return normalized


def _read_facts(con, bank, user_id, owners):
    if not owners or 'loan_repayments' not in bank_data.table_names(con):
        return []
    columns = ['user_id', 'row_key', 'occurrence'] + [
        name for name in LoanRepaymentFact.model_fields if name not in {'id', 'bank', 'component_overrides'}]
    rows = con.execute(
        f"SELECT {', '.join(columns)} FROM loan_repayments WHERE user_id = ? "
        f"AND source_account_id IN ({','.join('?' for _ in owners)})",
        (user_id, *sorted(owners)),
    ).fetchall()
    overrides = {}
    if 'loan_transaction_overrides' in bank_data.table_names(con):
        for row in con.execute(
            'SELECT source_account_id, fact_id, component, override_json FROM loan_transaction_overrides '
            f"WHERE user_id=? AND source_account_id IN ({','.join('?' for _ in owners)})",
            (user_id, *sorted(owners)),
        ).fetchall():
            overrides.setdefault((row['source_account_id'], row['fact_id']), {})[row['component']] = json.loads(row['override_json'])
    facts = []
    for row in rows:
        values = dict(row)
        identity = [bank, *[values[k] for k in ('user_id', 'source_account_id', 'account_no',
                    'sub_account', 'currency', 'row_key', 'occurrence')]]
        fact_id = 'loan:v1:' + sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        facts.append(LoanRepaymentFact(id=fact_id, bank=bank,
            component_overrides=overrides.get((values['source_account_id'], fact_id), {}), **values))
    return sorted(facts, key=lambda fact: fact.id)


class LoansReadMixin:
    def list_loan_repayments(self, *, bank: str, user_id: int,
                             source_account_id: int | None = None) -> list[LoanRepaymentFact]:
        owners = {a.id for a in AccountsRepo().list_for_user(user_id) if a.bank == bank}
        if source_account_id is not None:
            owners &= {source_account_id}
        if not owners:
            return []
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            return _read_facts(con, bank, user_id, owners)
        finally:
            con.close()

    def get_loan_repayment(self, *, bank: str, user_id: int, fact_id: str) -> LoanRepaymentFact | None:
        return next((fact for fact in self.list_loan_repayments(bank=bank, user_id=user_id)
                     if fact.id == fact_id), None)

    def update_loan_transaction(self, *, bank: str, user_id: int, txn_id: str, changes: dict) -> LoanRepaymentFact:
        from .transactions import TxnNotFound
        changes = validate_loan_edit(changes, require_fields=True)
        if not isinstance(txn_id, str) or not re.fullmatch(r'loan:v1:[0-9a-f]{64}:(principal|interest|penalty)', txn_id):
            raise TxnNotFound(bank, 'loan_repayment', txn_id)
        fact_id, component = txn_id.rsplit(':', 1)
        with self.transaction(bank=bank) as tx:
            con = tx._con
            # ponytail: bank-wide edit lock; use per-fact locks if write throughput matters.
            if db.DB_BACKEND == 'postgres':
                con.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ('loan-user-edits:' + bank,))
            else:
                con.execute('BEGIN IMMEDIATE')
            owners = {a.id for a in AccountsRepo().list_for_user(user_id) if a.bank == bank}
            # Raw observations are MVCC reads, not locks: their replacement order is independent.
            # Only metadata writers share our lock; an observation may disappear after this snapshot.
            fact = next((f for f in _read_facts(con, bank, user_id, owners) if f.id == fact_id), None)
            if fact is None or not Decimal(getattr(fact, component)):
                raise TxnNotFound(bank, 'loan_repayment', txn_id)
            merged = {**fact.component_overrides.get(component, {}), **changes}
            con.execute('CREATE TABLE IF NOT EXISTS loan_transaction_overrides ('
                'user_id INTEGER NOT NULL, source_account_id INTEGER NOT NULL, fact_id TEXT NOT NULL, '
                'component TEXT NOT NULL, override_json TEXT NOT NULL, '
                'PRIMARY KEY(user_id, source_account_id, fact_id, component))')
            con.execute('INSERT INTO loan_transaction_overrides '
                '(user_id, source_account_id, fact_id, component, override_json) VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(user_id, source_account_id, fact_id, component) '
                'DO UPDATE SET override_json=excluded.override_json',
                (user_id, fact.source_account_id, fact.id, component, json.dumps(merged, ensure_ascii=False)))
            fact.component_overrides[component] = merged
        return fact


def loan_transactions(fact: LoanRepaymentFact, excluded: bool = False) -> list[dict]:
    rows = []
    for component, description, category, subcategory in (
        ('principal', '貸款還本金', '還款', '本金'),
        ('interest', '貸款利息', '金融', '貸款利息'),
        ('penalty', '貸款違約金', '金融', '違約金'),
    ):
        magnitude = Decimal(getattr(fact, component))
        if not magnitude:
            continue
        principal = component == 'principal'
        amount = decimal_string(magnitude)
        rows.append(dict(
            id=f'{fact.id}:{component}', kind='loan_repayment', component=component,
            bank=fact.bank, source_account_id=fact.source_account_id,
            account_no=fact.account_no, account_or_card=fact.account_no, currency=fact.currency,
            date=fact.paid_on, datetime=None, description=description, display_description=description,
            category=category, subcategory=subcategory, amount=amount if principal else '-' + amount,
            display_amount=amount, display_sign='+' if principal else '-',
            cashflow_direction='neutral' if principal else 'expense', cashflow_amount='0' if principal else amount,
            flow_type='transfer' if principal else 'expense', read_only=False,
            reconciliation_status='unverified', excluded=excluded, auto_excluded=False,
            loan_repayment=fact.model_dump(),
        ))
        rows[-1].update(fact.component_overrides.get(component, {}))
    return rows
