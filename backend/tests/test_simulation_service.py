import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.budget import Budget
from app.models.fx_rate import FxRate
from app.models.transaction import Transaction
from app.schemas.transaction import TransactionCreate
from app.services import transaction_service
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


@pytest_asyncio.fixture
async def food_budget(session: AsyncSession, test_user, test_workspace, test_categories) -> Budget:
    """R$800 budget on the first test category (Alimentacao) for this month."""
    budget = Budget(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        category_id=test_categories[0].id,
        amount=Decimal("800.00"),
        month=date.today().replace(day=1),
        currency="BRL",
        amount_primary=Decimal("800.00"),
    )
    session.add(budget)
    await session.commit()
    await session.refresh(budget)
    return budget


@pytest.mark.asyncio
async def test_budget_block_reports_spend_before_and_after(
    session: AsyncSession, test_user, test_workspace, test_categories, sim_account, food_budget
):
    session.add(
        Transaction(
            id=uuid.uuid4(),
            user_id=test_user.id,
            account_id=sim_account.id,
            category_id=test_categories[0].id,
            description="IFOOD",
            amount=Decimal("700"),
            date=date.today(),
            type="debit",
            source="manual",
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today(), account=sim_account, category=test_categories[0],
    )

    assert impact.budget is not None
    assert impact.budget.category_name == test_categories[0].name
    assert impact.budget.limit == pytest.approx(800.0)
    assert impact.budget.spent_before == pytest.approx(700.0)
    assert impact.budget.spent_after == pytest.approx(850.0)
    assert impact.budget.remaining_after == pytest.approx(-50.0)
    assert impact.budget.exceeds_budget is True


@pytest.mark.asyncio
async def test_budget_block_absent_without_a_budget(
    session: AsyncSession, test_user, test_workspace, test_categories, sim_account
):
    """Category exists but has no budget - no implicit yardstick is invented."""
    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today(), account=sim_account, category=test_categories[0],
    )

    assert impact.budget is None
    assert impact.balance is not None


@pytest.mark.asyncio
async def test_budget_block_absent_without_a_category(
    session: AsyncSession, test_user, test_workspace, sim_account, food_budget
):
    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today(), account=sim_account, category=None,
    )

    assert impact.budget is None


@pytest.mark.asyncio
async def test_income_never_touches_the_budget(
    session: AsyncSession, test_user, test_workspace, test_categories, sim_account, food_budget
):
    """Budgets track expenses. Income must not consume one."""
    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("500"), currency="BRL", type="credit",
        tx_date=date.today(), account=sim_account, category=test_categories[0],
    )

    assert impact.budget is None


@pytest.mark.asyncio
async def test_exceeds_budget_flips_on_the_exact_cent(
    session: AsyncSession, test_user, test_workspace, test_categories, sim_account, food_budget
):
    """800.00 spent against an 800.00 limit is not over. One cent more is."""
    impact_at_limit = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("800.00"), currency="BRL", type="debit",
        tx_date=date.today(), account=sim_account, category=test_categories[0],
    )
    assert impact_at_limit.budget.exceeds_budget is False
    assert impact_at_limit.budget.remaining_after == pytest.approx(0.0)

    impact_over = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("800.01"), currency="BRL", type="debit",
        tx_date=date.today(), account=sim_account, category=test_categories[0],
    )
    assert impact_over.budget.exceeds_budget is True


@pytest_asyncio.fixture
async def sim_card(session: AsyncSession, test_user, test_workspace) -> Account:
    """Credit card closing on the 20th, due on the 1st, R$3000 limit, R$500 owed.

    The R$500 owed comes from a real posted transaction, not from the
    `balance` column - which is left at 0.00 on purpose. `create_account`
    writes that column once and the transaction path never updates it, so a
    manual card's column is its *opening* balance forever. Anything reading it
    as the current debt reports 3000 available here instead of 2500 and fails.
    """
    card = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Cartao",
        type="credit_card",
        balance=Decimal("0.00"),
        currency="BRL",
        credit_limit=Decimal("3000.00"),
        statement_close_day=20,
        payment_due_day=1,
    )
    session.add(card)
    await session.commit()
    session.add(
        Transaction(
            id=uuid.uuid4(),
            user_id=test_user.id,
            account_id=card.id,
            description="Compras anteriores",
            amount=Decimal("500"),
            currency="BRL",
            date=date.today().replace(day=1),
            type="debit",
            source="manual",
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    await session.refresh(card)
    return card


@pytest.mark.asyncio
async def test_accrual_mode_buckets_the_card_purchase_into_the_bill_month(
    session: AsyncSession, test_user, test_workspace, test_categories,
    sim_card, food_budget, monkeypatch,
):
    """In accrual mode the purchase counts in the bill month, so this month's
    budget - which is where `food_budget` lives - must be untouched."""
    async def _accrual(_session):
        return "accrual"

    monkeypatch.setattr(
        "app.services.admin_service.get_credit_card_accounting_mode", _accrual
    )

    # A purchase on the 21st closes after the 20th, so it lands on a later bill.
    purchase = date.today().replace(day=21)

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=purchase, account=sim_card, category=test_categories[0],
    )

    # The budget row consulted is the bill month's, which has no budget set.
    assert impact.budget is None


@pytest.mark.asyncio
async def test_card_purchase_reports_bill_date_and_remaining_limit(
    session: AsyncSession, test_user, test_workspace, sim_card
):
    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today().replace(day=5), account=sim_card,
    )

    assert impact.credit_card is not None
    # R$3000 limit with R$500 owed leaves R$2500; the pizza takes it to R$2350.
    assert impact.credit_card.available_before == pytest.approx(2500.0)
    assert impact.credit_card.available_after == pytest.approx(2350.0)
    assert impact.credit_card.exceeds_credit_limit is False
    assert impact.credit_card.bill_due_date > date.today().replace(day=5)


@pytest.mark.asyncio
async def test_purchase_over_the_limit_is_flagged(
    session: AsyncSession, test_user, test_workspace, sim_card
):
    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("2600"), currency="BRL", type="debit",
        tx_date=date.today().replace(day=5), account=sim_card,
    )

    assert impact.credit_card.exceeds_credit_limit is True
    assert impact.credit_card.available_after == pytest.approx(-100.0)


@pytest.mark.asyncio
async def test_card_without_a_limit_still_reports_the_bill_date(
    session: AsyncSession, test_user, test_workspace
):
    card = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Cartao sem limite",
        type="credit_card",
        balance=Decimal("0.00"),
        currency="BRL",
        credit_limit=None,
        statement_close_day=20,
        payment_due_day=1,
    )
    session.add(card)
    await session.commit()

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today().replace(day=5), account=card,
    )

    assert impact.credit_card is not None
    assert impact.credit_card.bill_due_date is not None
    assert impact.credit_card.available_before is None
    assert impact.credit_card.available_after is None
    assert impact.credit_card.exceeds_credit_limit is False


@pytest.mark.asyncio
async def test_non_card_account_has_no_credit_card_block(
    session: AsyncSession, test_user, test_workspace, sim_account
):
    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today(), account=sim_account,
    )

    assert impact.credit_card is None


async def _usd_card(session: AsyncSession, test_user, test_workspace) -> Account:
    """USD card, 3000 USD limit, 500 USD already owed via a real transaction.

    Same reasoning as `sim_card`: the debt lives in the ledger, not in the
    `balance` column, so the resolved-balance path is what is under test.
    """
    card = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Cartao USD",
        type="credit_card",
        balance=Decimal("0.00"),
        currency="USD",
        credit_limit=Decimal("3000.00"),
        statement_close_day=20,
        payment_due_day=1,
    )
    session.add(card)
    await session.commit()
    session.add(
        Transaction(
            id=uuid.uuid4(),
            user_id=test_user.id,
            account_id=card.id,
            description="Compras anteriores",
            amount=Decimal("500"),
            currency="USD",
            date=date.today().replace(day=1),
            type="debit",
            source="manual",
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    await session.refresh(card)
    return card


@pytest.mark.asyncio
async def test_card_currency_differs_from_primary_stays_in_its_own_currency(
    session: AsyncSession, test_user, test_workspace
):
    """`account.balance` and `account.credit_limit` are stored in the card's
    own currency, never the user's primary one - regression guard for a bug
    where the credit-card math used the primary-currency-converted amount,
    silently mixing units whenever the two currencies differ.

    A non-1:1 USD->BRL rate is seeded so the two code paths diverge sharply:
    the correct path spends 150 USD against a USD balance/limit (available
    after = 2350); the buggy path would instead spend the BRL-converted
    750 as if it were 750 USD (available after = 1750). Either bug (wrong
    amount or wrong currency label) fails one of the assertions below.
    """
    session.add(
        FxRate(
            base_currency="USD",
            quote_currency="BRL",
            date=date.today(),
            rate=Decimal("5.0"),
            source="test",
        )
    )

    card = await _usd_card(session, test_user, test_workspace)

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="USD", type="debit",
        tx_date=date.today().replace(day=5), account=card,
    )

    assert impact.credit_card is not None
    assert impact.credit_card.currency == "USD"
    # 3000 USD limit with 500 USD owed leaves 2500; the purchase takes it to
    # 2350 - in USD, not the ~1750 a BRL-converted (750) spend would produce.
    assert impact.credit_card.available_before == pytest.approx(2500.0)
    assert impact.credit_card.available_after == pytest.approx(2350.0)
    assert impact.credit_card.exceeds_credit_limit is False
    # The balance block still reports in the user's primary currency.
    assert impact.balance.currency == "BRL"


@pytest.mark.asyncio
async def test_simulation_matches_reality_after_booking(
    session: AsyncSession, test_user, test_workspace, sim_account
):
    """Simulate, then really create the transaction. The numbers must agree.

    If someone changes the projection engine later, this is the test that
    should break - not the user, at the end of the month.
    """
    from app.services.dashboard_service import _balance_at
    from app.services.transaction_calendar_service import get_transaction_calendar

    predicted = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today(), account=sim_account,
    )

    await transaction_service.create_transaction(
        session,
        test_workspace.id,
        test_user.id,
        TransactionCreate(
            description="Pizza",
            amount=Decimal("150"),
            date=date.today(),
            type="debit",
            account_id=sim_account.id,
            currency="BRL",
        ),
    )

    actual_today = await _balance_at(
        session, test_workspace.id, date.today(),
        primary_currency_hint="BRL", include_pending=True,
    )
    calendar = await get_transaction_calendar(
        session, test_workspace.id, test_user.id, month=date.today().replace(day=1)
    )
    actual_month_end = [d for d in calendar.days if d.in_month][-1].ending_balance

    assert actual_today == pytest.approx(predicted.balance.today_after, abs=0.01)
    assert actual_month_end == pytest.approx(predicted.balance.month_end_after, abs=0.01)


@pytest.mark.asyncio
async def test_simulating_never_writes(
    session: AsyncSession, test_user, test_workspace, test_categories, sim_account, food_budget
):
    from sqlalchemy import func, select

    async def _count() -> int:
        return await session.scalar(select(func.count()).select_from(Transaction))

    before = await _count()

    await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today(), account=sim_account, category=test_categories[0],
    )

    assert await _count() == before


@pytest.mark.asyncio
async def test_cross_currency_purchase_never_touches_network(
    session: AsyncSession, test_user, test_workspace, monkeypatch
):
    """A hypothetical purchase in a currency other than primary must resolve
    via stored rates only.

    Regression guard for the read-only constraint: the USD->BRL rate is seeded
    straight into `fx_rates`, and `sync_rates()` - a DB upsert + commit plus an
    outbound HTTP call to the FX provider - is patched to record any call. The
    conversion has to come entirely from the cache.

    The rate is a real 5.0, and the assertions below are on the *converted*
    number. This test used to seed nothing and assert the 1:1 fallback
    (100 USD -> R$100) as correct, pinning a ~5x error as expected behaviour
    on the one screen where the user decides based on the figure.

    The account's one posted transaction is stamped `currency="BRL"`
    explicitly (matching the account and the user's primary currency) so the
    only currency mismatch in play is the hypothetical purchase's own
    `currency="USD"` - keeping this test isolated to the one conversion this
    task controls.
    """
    session.add(
        FxRate(
            base_currency="USD",
            quote_currency="BRL",
            date=date.today(),
            rate=Decimal("5.0"),
            source="test",
        )
    )

    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Simulacao FX",
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
            currency="BRL",
            date=date.today().replace(day=1),
            type="credit",
            source="manual",
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    await session.refresh(account)

    import app.services.fx_rate_service as fx_rate_service

    calls: list[tuple] = []

    async def _tracking_sync_rates(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(fx_rate_service, "sync_rates", _tracking_sync_rates)

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("100"), currency="USD", type="debit",
        tx_date=date.today(), account=account,
    )

    assert calls == []  # never reached the write/network path
    assert impact.balance.currency == "BRL"
    assert impact.balance.today_before == pytest.approx(2000.0)
    # 100 USD at the stored 5.0 rate is R$500, so 2000 -> 1500. Never 1900,
    # which is what the silent 1:1 fallback produced.
    assert impact.balance.today_after == pytest.approx(1500.0)
    assert impact.balance.month_end_after == pytest.approx(1500.0)
    assert impact.balance.ends_month_negative is False


@pytest.mark.asyncio
async def test_missing_rate_refuses_to_report_a_1_to_1_impact(
    session: AsyncSession, test_user, test_workspace, sim_account, monkeypatch
):
    """No cached rate and no fetching allowed -> no number at all.

    `get_rate` answers a cache miss with a 1:1 fallback, which is the right
    call for a dashboard that must still render something and the wrong call
    here: a USD purchase counted as if 1 USD were 1 BRL is a ~5x error on the
    figure the user is deciding against. The service raises instead, and
    `propose_create_transaction` degrades to `impact: None` - the proposal
    survives and the card simply shows no impact lines.

    `sync_rates` is still patched to prove the refusal is not a disguised
    network attempt: the read-only guarantee holds either way.
    """
    import app.services.fx_rate_service as fx_rate_service
    from app.services.simulation_service import MissingRateError

    calls: list[tuple] = []

    async def _tracking_sync_rates(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(fx_rate_service, "sync_rates", _tracking_sync_rates)

    with pytest.raises(MissingRateError):
        await simulate_transaction(
            session, test_workspace.id, test_user.id,
            amount=Decimal("100"), currency="USD", type="debit",
            tx_date=date.today(), account=sim_account,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_connected_card_debt_is_positive_and_still_shrinks_availability(
    session: AsyncSession, test_user, test_workspace
):
    """A provider-synced card stores its debt as a *positive* balance.

    Every other consumer negates it (`account_service.py:165`,
    `dashboard_service.py:1393-1395`). Reading the column raw makes
    `compute_available_credit` see a positive balance, compute `utilized = 0`,
    and report the full limit with `exceeds_credit_limit=False` - forever, for
    every connected card, on the exact number the user asked about.
    """
    from app.models.bank_connection import BankConnection

    connection = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        provider="pluggy",
        external_id="item-1",
        institution_name="Banco Teste",
    )
    session.add(connection)
    await session.commit()

    card = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        connection_id=connection.id,
        name="Cartao conectado",
        type="credit_card",
        balance=Decimal("500.00"),  # provider convention: debt is positive
        currency="BRL",
        credit_limit=Decimal("3000.00"),
        statement_close_day=20,
        payment_due_day=1,
    )
    session.add(card)
    await session.commit()
    await session.refresh(card)

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today().replace(day=5), account=card,
    )

    assert impact.credit_card is not None
    # 3000 limit, 500 owed -> 2500 available, and the pizza takes 150 of it.
    assert impact.credit_card.available_before == pytest.approx(2500.0)
    assert impact.credit_card.available_after == pytest.approx(2350.0)
    # Never the untouched full limit, which is what the raw column produced.
    assert impact.credit_card.available_before != pytest.approx(3000.0)
    assert impact.credit_card.available_after < impact.credit_card.available_before


@pytest.mark.asyncio
async def test_manual_card_debt_comes_from_transactions_not_the_balance_column(
    session: AsyncSession, test_user, test_workspace
):
    """`Account.balance` is written once at creation and never by the tx path.

    The column here is deliberately stale (0.00, an untouched opening balance)
    while the ledger carries R$1,200 of purchases. Reading the column reports
    the full R$3,000 limit as available; reading the ledger reports R$1,800.
    """
    card = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Cartao manual",
        type="credit_card",
        balance=Decimal("0.00"),
        currency="BRL",
        credit_limit=Decimal("3000.00"),
        statement_close_day=20,
        payment_due_day=1,
    )
    session.add(card)
    await session.commit()
    for amount in (Decimal("900"), Decimal("300")):
        session.add(
            Transaction(
                id=uuid.uuid4(),
                user_id=test_user.id,
                account_id=card.id,
                description="Compra",
                amount=amount,
                currency="BRL",
                date=date.today().replace(day=1),
                type="debit",
                source="manual",
                created_at=datetime.now(timezone.utc),
            )
        )
    await session.commit()
    await session.refresh(card)

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today().replace(day=5), account=card,
    )

    assert impact.credit_card is not None
    assert impact.credit_card.available_before == pytest.approx(1800.0)
    assert impact.credit_card.available_after == pytest.approx(1650.0)
    assert impact.credit_card.available_before != pytest.approx(3000.0)


@pytest.mark.asyncio
async def test_card_without_cycle_days_reports_no_bill_date(
    session: AsyncSession, test_user, test_workspace
):
    """`statement_close_day` / `payment_due_day` are nullable and commonly unset.

    `compute_effective_date` returns `tx_date` unchanged without them, so the
    card would tell the user the bill is due the very day they buy the pizza.
    Report no date instead; the limit information still stands.
    """
    card = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Cartao sem ciclo",
        type="credit_card",
        balance=Decimal("0.00"),
        currency="BRL",
        credit_limit=Decimal("3000.00"),
        statement_close_day=None,
        payment_due_day=None,
    )
    session.add(card)
    await session.commit()

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today(), account=card,
    )

    assert impact.credit_card is not None
    assert impact.credit_card.bill_due_date is None
    # The limit half of the block is unaffected by the missing cycle metadata.
    assert impact.credit_card.available_before == pytest.approx(3000.0)
    assert impact.credit_card.available_after == pytest.approx(2850.0)


@pytest.mark.asyncio
async def test_transaction_currency_differs_from_the_card_currency(
    session: AsyncSession, test_user, test_workspace
):
    """A BRL purchase on a USD card must be converted into the card's currency.

    This is the branch that actually mixes units (`simulation_service.py`'s
    tx-currency != card-currency path); the sibling test above only exercises
    the short-circuit where the two already match. At USD->BRL 5.0 the R$150
    pizza is 30 USD, so 2500 available becomes 2470 - not 2350 (the amount
    used raw as if BRL were USD).
    """
    session.add(
        FxRate(
            base_currency="USD",
            quote_currency="BRL",
            date=date.today(),
            rate=Decimal("5.0"),
            source="test",
        )
    )

    card = await _usd_card(session, test_user, test_workspace)

    impact = await simulate_transaction(
        session, test_workspace.id, test_user.id,
        amount=Decimal("150"), currency="BRL", type="debit",
        tx_date=date.today().replace(day=5), account=card,
    )

    assert impact.credit_card is not None
    assert impact.credit_card.currency == "USD"
    assert impact.credit_card.available_before == pytest.approx(2500.0)
    assert impact.credit_card.available_after == pytest.approx(2470.0)
    assert impact.credit_card.exceeds_credit_limit is False


@pytest.mark.asyncio
async def test_unsupported_transaction_type_is_rejected(
    session: AsyncSession, test_user, test_workspace, sim_account
):
    """The MCP JSON schema declares the enum but nothing enforces it.

    `registry.call_tool` splats `**arguments` straight into the handler, so a
    model emitting `type="expense"` would otherwise fall into the `credit`
    branch and show the balance *rising* by R$150 for a pizza.
    """
    with pytest.raises(ValueError, match="unsupported transaction type"):
        await simulate_transaction(
            session, test_workspace.id, test_user.id,
            amount=Decimal("150"), currency="BRL", type="expense",
            tx_date=date.today(), account=sim_account,
        )
