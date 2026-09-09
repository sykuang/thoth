"""Read-only, credential-owned native loan observations and their projection."""
from decimal import Decimal
from hashlib import sha256
import json
from pydantic import BaseModel, field_validator
from backend.core import bank_data
from backend.server import db
from backend.server.creds_store import AccountsRepo


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

    @field_validator('principal', 'interest', 'penalty', 'paid_total', 'principal_balance')
    @classmethod
    def validate_amount(cls, value: str) -> str:
        normalized = decimal_string(value)
        if Decimal(normalized) < 0:
            raise ValueError('negative loan amount')
        return normalized


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
            if 'loan_repayments' not in bank_data.table_names(con):
                return []
            columns = ['user_id', 'row_key', 'occurrence'] + [
                name for name in LoanRepaymentFact.model_fields if name not in {'id', 'bank'}]
            rows = con.execute(
                f"SELECT {', '.join(columns)} FROM loan_repayments WHERE user_id = ? "
                f"AND source_account_id IN ({','.join('?' for _ in owners)})",
                (user_id, *sorted(owners)),
            ).fetchall()
            facts = []
            for row in rows:
                values = dict(row)
                identity = [bank, *[values[k] for k in ('user_id', 'source_account_id', 'account_no',
                            'sub_account', 'currency', 'row_key', 'occurrence')]]
                facts.append(LoanRepaymentFact(
                    id='loan:v1:' + sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
                    bank=bank, **values))
            return sorted(facts, key=lambda fact: fact.id)
        finally:
            con.close()

    def get_loan_repayment(self, *, bank: str, user_id: int, fact_id: str) -> LoanRepaymentFact | None:
        return next((fact for fact in self.list_loan_repayments(bank=bank, user_id=user_id)
                     if fact.id == fact_id), None)


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
            flow_type='transfer' if principal else 'expense', read_only=True,
            reconciliation_status='unverified', excluded=excluded, auto_excluded=False,
            loan_repayment=fact.model_dump(),
        ))
    return rows
