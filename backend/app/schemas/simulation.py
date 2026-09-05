from datetime import date as _Date
from typing import Optional

from pydantic import BaseModel


class BudgetImpact(BaseModel):
    category_name: str
    limit: float
    spent_before: float
    spent_after: float
    remaining_after: float  # negative when over budget
    exceeds_budget: bool
    currency: str


class BalanceImpact(BaseModel):
    today_before: float
    today_after: float
    month_end_before: float
    month_end_after: float
    ends_month_negative: bool
    currency: str


class CreditCardImpact(BaseModel):
    bill_due_date: _Date
    available_before: Optional[float] = None
    available_after: Optional[float] = None
    exceeds_credit_limit: bool = False


class TransactionImpact(BaseModel):
    balance: BalanceImpact
    budget: Optional[BudgetImpact] = None
    credit_card: Optional[CreditCardImpact] = None
