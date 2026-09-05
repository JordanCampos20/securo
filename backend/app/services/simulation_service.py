"""Impact of a hypothetical transaction - read-only, never persisted.

A hypothetical row does not require running the projection twice. Balance is
a running total by `Transaction.date`, so a R$150 purchase today shifts the
whole curve by -150 from that date on. We read the "before" once and apply
the delta arithmetically: same answer, half the queries.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.account import Account
from app.models.category import Category
from app.models.user import User
from app.schemas.simulation import (
    BalanceImpact,
    BudgetImpact,
    CreditCardImpact,
    TransactionImpact,
)


def _credit_card_impact(
    account: Account,
    *,
    tx_date: date,
    type: str,
    amount_primary: Decimal,
) -> Optional[CreditCardImpact]:
    """Which bill the purchase lands on, and what it does to the limit.

    Context only - this never feeds the balance or budget numbers. A card
    with no limit configured still reports its bill date; there is simply
    nothing to measure the available credit against.
    """
    if account.type != "credit_card":
        return None

    from app.services.credit_card_service import (
        compute_available_credit,
        compute_effective_date,
    )

    bill_due_date = compute_effective_date(
        tx_date,
        getattr(account, "statement_close_day", None),
        getattr(account, "payment_due_day", None),
    )

    limit = getattr(account, "credit_limit", None)
    if limit is None:
        return CreditCardImpact(
            bill_due_date=bill_due_date,
            available_before=None,
            available_after=None,
            exceeds_credit_limit=False,
        )

    current = Decimal(str(account.balance))
    spend = amount_primary if type == "debit" else -amount_primary
    before = compute_available_credit(Decimal(str(limit)), current)
    after = compute_available_credit(Decimal(str(limit)), current - spend)

    return CreditCardImpact(
        bill_due_date=bill_due_date,
        available_before=round(float(before), 2),
        available_after=round(float(after), 2),
        exceeds_credit_limit=after < 0,
    )


async def simulate_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    amount: Decimal,
    currency: str,
    type: Literal["debit", "credit"],
    tx_date: date,
    account: Account,
    category: Optional[Category] = None,
) -> TransactionImpact:
    from app.services.dashboard_service import _balance_at
    from app.services.fx_rate_service import convert
    from app.services.transaction_calendar_service import get_transaction_calendar

    user = await session.get(User, user_id)
    primary = user.primary_currency if user else get_settings().default_currency

    if currency == primary:
        amount_primary = Decimal(str(amount))
    else:
        converted, _ = await convert(session, Decimal(str(amount)), currency, primary)
        amount_primary = converted

    # Debit takes money out, credit puts it in. The sign is the whole trick.
    delta = float(-amount_primary if type == "debit" else amount_primary)

    today = date.today()
    today_before = await _balance_at(
        session, workspace_id, today, primary_currency_hint=primary, include_pending=True
    )
    # A future-dated purchase has not happened yet, so today is untouched.
    today_after = today_before + delta if tx_date <= today else today_before

    # The calendar is always built for the month of the purchase, not the
    # current month, so "month end" means the end of the month being asked about.
    calendar = await get_transaction_calendar(
        session, workspace_id, user_id, month=tx_date.replace(day=1)
    )
    in_month = [d for d in calendar.days if d.in_month]
    month_end_before = in_month[-1].ending_balance if in_month else today_before
    month_end_after = month_end_before + delta

    from app.services.admin_service import get_credit_card_accounting_mode

    # Mirror reporting_date_col(): 'accrual' buckets by the bill date,
    # 'cash' (the default) by the purchase date. Naming is inverted from
    # the accounting jargon - see _query_filters.py:89-92.
    accounting_mode = await get_credit_card_accounting_mode(session)
    bucket_date = tx_date
    if accounting_mode == "accrual" and account.type == "credit_card":
        from app.services.credit_card_service import compute_effective_date

        bucket_date = compute_effective_date(
            tx_date,
            getattr(account, "statement_close_day", None),
            getattr(account, "payment_due_day", None),
        )

    budget = await _budget_impact(
        session, workspace_id, user_id,
        category=category, type=type, amount_primary=amount_primary,
        budget_month=bucket_date.replace(day=1), primary=primary,
    )

    credit_card = _credit_card_impact(
        account, tx_date=tx_date, type=type, amount_primary=amount_primary
    )

    return TransactionImpact(
        balance=BalanceImpact(
            today_before=round(today_before, 2),
            today_after=round(today_after, 2),
            month_end_before=round(month_end_before, 2),
            month_end_after=round(month_end_after, 2),
            ends_month_negative=month_end_after < 0,
            currency=primary,
        ),
        budget=budget,
        credit_card=credit_card,
    )


async def _budget_impact(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    category: Optional[Category],
    type: str,
    amount_primary: Decimal,
    budget_month: date,
    primary: str,
) -> Optional[BudgetImpact]:
    """The category's budget before and after the hypothetical spend.

    Returns None whenever there is no yardstick to measure against: no
    category, no budget on it, or an income row (budgets track expenses).
    """
    if category is None or type != "debit":
        return None

    from app.services.budget_service import get_budget_vs_actual

    rows = await get_budget_vs_actual(session, workspace_id, user_id, month=budget_month)
    row = next((r for r in rows if r.category_id == category.id), None)
    if row is None or row.budget_amount is None:
        return None

    limit = float(row.budget_amount)
    spent_before = float(row.actual_amount)
    spent_after = spent_before + float(amount_primary)

    return BudgetImpact(
        category_name=row.category_name,
        limit=round(limit, 2),
        spent_before=round(spent_before, 2),
        spent_after=round(spent_after, 2),
        remaining_after=round(limit - spent_after, 2),
        exceeds_budget=spent_after > limit,
        currency=primary,
    )
