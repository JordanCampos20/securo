import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.transaction import Transaction
from app.services.simulation_service import simulate_transaction


@pytest_asyncio.fixture
async def sim_account(session: AsyncSession, test_user, test_workspace) -> Account:
    """Manual BRL account funded with a single posted credit of 2000."""
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Simulacao",
        type="checking",
        balance=Decimal("0.00"),
        currency="BRL",
    )
    session.add(account)
    await session.commit()
    session.add(
        Transaction(
            id=uuid.uuid4(),
            user_id=test_user.id,
            account_id=account.id,
            description="Salario",
            amount=Decimal("2000"),
            date=date.today().replace(day=1),
            type="credit",
            source="manual",
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    await session.refresh(account)
    return account


@pytest.mark.asyncio
async def test_debit_today_lowers_today_and_month_end(
    session: AsyncSession, test_user, test_workspace, sim_account
):
    impact = await simulate_transaction(
        session,
        test_workspace.id,
        test_user.id,
        amount=Decimal("150"),
        currency="BRL",
        type="debit",
        tx_date=date.today(),
        account=sim_account,
    )

    assert impact.balance.today_before == pytest.approx(2000.0)
    assert impact.balance.today_after == pytest.approx(1850.0)
    assert impact.balance.month_end_before == pytest.approx(2000.0)
    assert impact.balance.month_end_after == pytest.approx(1850.0)
    assert impact.balance.ends_month_negative is False
    assert impact.balance.currency == "BRL"
    assert impact.budget is None
    assert impact.credit_card is None


@pytest.mark.asyncio
async def test_future_purchase_leaves_today_untouched(
    session: AsyncSession, test_user, test_workspace, sim_account
):
    """A purchase a few days out does not move today, but does move month end.

    `today + 3 days` is always strictly after today, so today is untouched
    regardless of what day the suite happens to run on. Month end is measured
    for the purchase's own month (the calendar is built for `tx_date`'s
    month), so the delta lands there unconditionally even if the purchase
    date falls in the following calendar month - there is no "spills into
    next month" hazard to guard against here.
    """
    future = date.today() + timedelta(days=3)

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=future, account=sim_account,
    )

    assert impact.balance.today_after == impact.balance.today_before
    assert impact.balance.month_end_after == pytest.approx(
        impact.balance.month_end_before - 150.0
    )


@pytest.mark.asyncio
async def test_income_raises_the_balance(
    session: AsyncSession, test_user, test_workspace, sim_account
):
    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("500"), currency="BRL", type="credit",
        tx_date=date.today(), account=sim_account,
    )

    assert impact.balance.today_after == pytest.approx(2500.0)
    assert impact.balance.month_end_after == pytest.approx(2500.0)
