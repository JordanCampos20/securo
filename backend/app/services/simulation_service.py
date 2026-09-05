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


class MissingRateError(RuntimeError):
    """No stored FX rate could be resolved for a conversion the impact needs.

    The simulation converts with ``allow_fetch=False`` (read-only path), so a
    cache miss cannot be repaired by calling the provider. `get_rate` would
    hand back a 1:1 fallback here, which on this screen means showing the user
    a confidently wrong number - a USD purchase counted as if 1 USD were 1 BRL
    is a ~5x error on the exact figure they are deciding against. Raising
    instead lets `propose_create_transaction` degrade to `impact: None`: the
    proposal survives, and the user sees no impact lines rather than false ones.
    """


async def _convert_or_raise(
    session: AsyncSession,
    amount: Decimal,
    from_currency: str,
    to_currency: str,
) -> Decimal:
    """Convert using stored rates only, or raise :class:`MissingRateError`.

    Deliberately calls `_resolve_rate` rather than `convert`/`get_rate`: those
    swallow a cache miss into a 1:1 fallback, which is right for a dashboard
    that must still render something and wrong for a number the user is about
    to make a purchase decision on.

    `allow_fetch=False` is the read-only guarantee - a fetch would `sync_rates()`,
    which does a DB upsert + commit and an outbound HTTP call.
    """
    if from_currency == to_currency:
        return Decimal(str(amount))

    from app.services.fx_rate_service import _resolve_rate

    rate = await _resolve_rate(session, from_currency, to_currency, allow_fetch=False)
    if rate is None:
        raise MissingRateError(
            f"no stored FX rate for {from_currency} -> {to_currency}; "
            "refusing to report a 1:1 impact"
        )
    return (Decimal(str(amount)) * rate).quantize(Decimal("0.01"))


def _credit_card_impact(
    account: Account,
    *,
    tx_date: date,
    type: str,
    amount_account_currency: Decimal,
    current_balance: Decimal,
) -> Optional[CreditCardImpact]:
    """Which bill the purchase lands on, and what it does to the limit.

    Context only - this never feeds the balance or budget numbers. A card
    with no limit configured still reports its bill date; there is simply
    nothing to measure the available credit against.

    `current_balance` and `account.credit_limit` are stored in the account's
    own currency, so the caller must pass the purchase amount already
    converted into that same currency - never the primary-currency amount,
    which would silently mix units whenever the card's currency differs from
    the user's primary one.

    `current_balance` must be the *resolved* balance, the way the rest of the
    app derives it - never the raw `account.balance` column. That column is
    written once at account creation for manual accounts and never updated by
    the transaction path, and for connected accounts the provider stores card
    debt as a *positive* number that every other consumer negates. Reading it
    directly reported the opening balance for manual cards and a permanent
    "full limit available, nothing to worry about" for connected ones.
    """
    if account.type != "credit_card":
        return None

    from app.services.credit_card_service import (
        compute_available_credit,
        compute_effective_date,
    )

    # Both cycle days are nullable and a hand-added card commonly has neither.
    # `compute_effective_date` returns `tx_date` unchanged in that case, which
    # would render as "Bill due <today>" - a date we invented. Report no bill
    # date instead and let the card drop the line.
    close_day = getattr(account, "statement_close_day", None)
    due_day = getattr(account, "payment_due_day", None)
    bill_due_date = (
        compute_effective_date(tx_date, close_day, due_day)
        if close_day and due_day
        else None
    )

    limit = getattr(account, "credit_limit", None)
    if limit is None:
        return CreditCardImpact(
            bill_due_date=bill_due_date,
            available_before=None,
            available_after=None,
            exceeds_credit_limit=False,
            currency=account.currency,
        )

    current = Decimal(str(current_balance))
    spend = amount_account_currency if type == "debit" else -amount_account_currency
    before = compute_available_credit(Decimal(str(limit)), current)
    after = compute_available_credit(Decimal(str(limit)), current - spend)

    return CreditCardImpact(
        bill_due_date=bill_due_date,
        available_before=round(float(before), 2),
        available_after=round(float(after), 2),
        exceeds_credit_limit=after < 0,
        currency=account.currency,
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
    from app.services.dashboard_service import (
        _balance_at,
        _total_balance_by_currency,
    )
    from app.services.transaction_calendar_service import get_transaction_calendar

    # The `type` enum is declared in the MCP JSON schema, but nothing validates
    # arguments against that schema before dispatch (`registry.py` splats
    # `**arguments` straight into the handler). Without this guard a model
    # emitting `type="expense"` falls into the `credit` branch and the card
    # cheerfully shows the balance *rising* by R$150 for a pizza.
    if type not in ("debit", "credit"):
        raise ValueError(f"unsupported transaction type: {type!r}")

    user = await session.get(User, user_id)
    primary = user.primary_currency if user else get_settings().default_currency

    # Read-only conversion: raises rather than falling back to 1:1. See
    # `_convert_or_raise`.
    amount_primary = await _convert_or_raise(session, amount, currency, primary)

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

    credit_card = None
    if account.type == "credit_card":
        amount_account_currency = await _convert_or_raise(
            session, amount, currency, account.currency
        )

        # Resolve the card's real debt the way the rest of the app does, in the
        # card's own currency. `_total_balance_by_currency` sums the signed
        # transactions for a manual account and negates the provider's positive
        # debt for a connected one - both of which `account.balance` gets wrong.
        # Only worth the query when there is a limit to measure against.
        current_balance = Decimal("0")
        if getattr(account, "credit_limit", None) is not None:
            totals = await _total_balance_by_currency(
                session, workspace_id, today, [account.id], include_pending=True
            )
            current_balance = Decimal(str(totals.get(account.currency, 0.0)))

        credit_card = _credit_card_impact(
            account,
            tx_date=tx_date,
            type=type,
            amount_account_currency=amount_account_currency,
            current_balance=current_balance,
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
