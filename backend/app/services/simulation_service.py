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
from app.schemas.simulation import BalanceImpact, TransactionImpact


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

    return TransactionImpact(
        balance=BalanceImpact(
            today_before=round(today_before, 2),
            today_after=round(today_after, 2),
            month_end_before=round(month_end_before, 2),
            month_end_after=round(month_end_after, 2),
            ends_month_negative=month_end_after < 0,
            currency=primary,
        ),
        budget=None,
        credit_card=None,
    )
