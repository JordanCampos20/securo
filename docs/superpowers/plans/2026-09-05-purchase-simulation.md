# Purchase Simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent answer "if I buy this pizza for R$150 on my credit card, where does that leave me?" with real numbers, and book it on confirm.

**Architecture:** One new read-only service, `simulation_service.py`, computes the impact of a hypothetical transaction by running the existing projection **once** and applying the delta arithmetically. Its result is attached to the existing `propose_create_transaction` MCP tool preview, which already renders an Apply button. No endpoint, no table, no migration.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / Pydantic / pytest-asyncio · React / TypeScript / vitest / i18next

**Spec:** `docs/superpowers/specs/2026-09-05-simulacao-de-compra-design.md`

## Global Constraints

- **Read-only.** `simulation_service` never writes, never opens a transaction, never persists a simulation. No Alembic migration in this plan.
- **Never override the global accounting mode.** Budget bucketing uses `reporting_date_col()` and honors `credit_card_accounting_mode` so the simulation always agrees with the Budgets screen. Note the inverted naming (`_query_filters.py:89-92`): `cash` buckets by `Transaction.date`, `accrual` by `effective_date`. Default is `cash`.
- **Facts, not sentences.** The service returns numbers and boolean flags. Prose is the LLM's job; layout is the card's job.
- **Blocks are all-or-nothing.** `budget` and `credit_card` are either fully populated or `None`. Never half-filled.
- **Reference month** for the calendar is always the month of `tx_date`, not the current month.
- **All 13 locales** must be updated together — `frontend/src/locales/i18n.test.ts` compares keys and placeholders across every file and fails otherwise.
- Commit convention: Conventional Commits, English, lower-case subject, no trailing period, <=72 chars. Never `git add -A`.
- Backend tests run from `backend/`: `pytest`. Frontend tests run from `frontend/`: `npm run test`.

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/schemas/simulation.py` (create) | The four Pydantic models of the impact contract |
| `backend/app/services/simulation_service.py` (create) | The whole calculation. Single public function |
| `backend/tests/test_simulation_service.py` (create) | Service coverage, including the equivalence and purity guards |
| `backend/mcp_server/tools/proposals.py` (modify) | Attach `impact` to the preview; teach the LLM to narrate it |
| `backend/tests/test_agents_mcp_tools.py` (modify) | `impact` is present; the `apply=true` write path is unchanged |
| `frontend/src/components/agents/proposal-card.tsx` (modify) | Render up to three impact lines |
| `frontend/src/components/agents/proposal-card.test.tsx` (create) | Lines appear and disappear with their blocks |
| `frontend/src/locales/*.json` (modify, 13 files) | `agents.proposal.impact.*` keys |

---

### Task 1: Balance impact

Vertical slice: the contract plus the balance block. `budget` and `credit_card` stay `None` until Tasks 2 and 3.

**Files:**
- Create: `backend/app/schemas/simulation.py`
- Create: `backend/app/services/simulation_service.py`
- Test: `backend/tests/test_simulation_service.py`

**Interfaces:**
- Consumes: `dashboard_service._balance_at()`, `transaction_calendar_service.get_transaction_calendar()`, `fx_rate_service.convert()`
- Produces: `simulate_transaction(session, workspace_id, user_id, *, amount: Decimal, currency: str, type: str, tx_date: date, account: Account, category: Category | None = None) -> TransactionImpact`, and the schemas `TransactionImpact`, `BalanceImpact`, `BudgetImpact`, `CreditCardImpact`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_simulation_service.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run from `backend/`: `pytest tests/test_simulation_service.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.simulation_service'`

- [ ] **Step 3: Write the schemas**

Create `backend/app/schemas/simulation.py`:

```python
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
```

- [ ] **Step 4: Write the minimal service**

Create `backend/app/services/simulation_service.py`:

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run from `backend/`: `pytest tests/test_simulation_service.py -v`

Expected: PASS

- [ ] **Step 6: Add the future-date and income tests**

Append to `backend/tests/test_simulation_service.py`:

```python
@pytest.mark.asyncio
async def test_future_purchase_leaves_today_untouched(
    session: AsyncSession, test_user, test_workspace, sim_account
):
    """A purchase a few days out does not move today, but does move month end.

    Anchored on day 2 of the month so adding 3 days cannot spill into the
    next month and silently change which month the calendar is built for.
    """
    future = date.today().replace(day=2) + timedelta(days=3)

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
```

- [ ] **Step 7: Run the full file**

Run from `backend/`: `pytest tests/test_simulation_service.py -v`

Expected: 3 passed

- [ ] **Step 8: Commit**

```bash
git add backend/app/schemas/simulation.py backend/app/services/simulation_service.py backend/tests/test_simulation_service.py
git commit -m "feat(simulation): compute balance impact of a hypothetical transaction"
```

---

### Task 2: Budget impact

**Files:**
- Modify: `backend/app/services/simulation_service.py`
- Test: `backend/tests/test_simulation_service.py`

**Interfaces:**
- Consumes: `simulate_transaction()` from Task 1; `budget_service.get_budget_vs_actual()`, which returns a `list[BudgetVsActual]` where each row carries `category_id`, `category_name`, `budget_amount: Decimal | None` (the limit, `None` when no budget exists) and `actual_amount: Decimal` (spent so far this month).
- Produces: `TransactionImpact.budget` populated as `BudgetImpact` when the category has a budget.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_simulation_service.py`. Add `from app.models.budget import Budget` to the imports at the top of the file.

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run from `backend/`: `pytest tests/test_simulation_service.py -v -k budget`

Expected: FAIL — `AttributeError: 'NoneType' object has no attribute 'category_name'` on the first test, because `budget` is still hardcoded to `None`.

- [ ] **Step 3: Implement the budget block**

In `backend/app/services/simulation_service.py`, add `BudgetImpact` to the schema import:

```python
from app.schemas.simulation import BalanceImpact, BudgetImpact, TransactionImpact
```

Add this helper below `simulate_transaction`:

```python
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
```

- [ ] **Step 4: Wire it into `simulate_transaction`**

The month a transaction is bucketed into follows the global accounting mode —
never a rule of our own, or the number would disagree with the Budgets screen.

Replace `budget=None,` in the returned `TransactionImpact` and add this above the `return`:

```python
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
```

Then pass `budget=budget,` in the `TransactionImpact(...)` call.

- [ ] **Step 5: Run the tests to verify they pass**

Run from `backend/`: `pytest tests/test_simulation_service.py -v`

Expected: 8 passed

- [ ] **Step 6: Add the accounting-mode test**

This is the test that pins the promise of agreeing with the Budgets screen. Append to `backend/tests/test_simulation_service.py`:

```python
@pytest_asyncio.fixture
async def sim_card(session: AsyncSession, test_user, test_workspace) -> Account:
    """Credit card closing on the 20th, due on the 1st, R$3000 limit, R$500 owed."""
    card = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Cartao",
        type="credit_card",
        balance=Decimal("-500.00"),
        currency="BRL",
        credit_limit=Decimal("3000.00"),
        statement_close_day=20,
        payment_due_day=1,
    )
    session.add(card)
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
    import app.services.simulation_service as sim

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
```

- [ ] **Step 7: Run the full file**

Run from `backend/`: `pytest tests/test_simulation_service.py -v`

Expected: 9 passed

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/simulation_service.py backend/tests/test_simulation_service.py
git commit -m "feat(simulation): add category budget impact"
```

---

### Task 3: Credit card impact

**Files:**
- Modify: `backend/app/services/simulation_service.py`
- Test: `backend/tests/test_simulation_service.py`

**Interfaces:**
- Consumes: `credit_card_service.compute_effective_date(tx_date, statement_close_day, payment_due_day) -> date` and `credit_card_service.compute_available_credit(credit_limit: Decimal | None, current_balance: Decimal) -> Decimal | None`; the `sim_card` fixture from Task 2.
- Produces: `TransactionImpact.credit_card` populated as `CreditCardImpact` when the account is a credit card.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_simulation_service.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run from `backend/`: `pytest tests/test_simulation_service.py -v -k card`

Expected: FAIL — `AssertionError: assert None is not None`, because `credit_card` is still hardcoded to `None`.

- [ ] **Step 3: Implement the credit card block**

In `backend/app/services/simulation_service.py`, extend the schema import:

```python
from app.schemas.simulation import (
    BalanceImpact,
    BudgetImpact,
    CreditCardImpact,
    TransactionImpact,
)
```

Add this helper:

```python
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
```

- [ ] **Step 4: Wire it in**

Above the `return` in `simulate_transaction`, add:

```python
    credit_card = _credit_card_impact(
        account, tx_date=tx_date, type=type, amount_primary=amount_primary
    )
```

and pass `credit_card=credit_card,` in the `TransactionImpact(...)` call, replacing `credit_card=None,`.

- [ ] **Step 5: Run the tests to verify they pass**

Run from `backend/`: `pytest tests/test_simulation_service.py -v`

Expected: 13 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/simulation_service.py backend/tests/test_simulation_service.py
git commit -m "feat(simulation): add credit card bill and limit impact"
```

---

### Task 4: Correctness guards

Two tests that pin the promises the whole feature rests on. They add no production code — if they fail, something in Tasks 1–3 is wrong.

**Files:**
- Test: `backend/tests/test_simulation_service.py`

**Interfaces:**
- Consumes: `simulate_transaction()` from Tasks 1–3; `transaction_service.create_transaction(session, workspace_id, user_id, TransactionCreate)` and `app.schemas.transaction.TransactionCreate`; the `sim_account` fixture.
- Produces: nothing new. Guard tests only.

- [ ] **Step 1: Write the equivalence guard**

This is the highest-value test in the plan. It proves the arithmetic delta shortcut tells the truth: simulate, then actually book the transaction, then read the real numbers back and compare.

Append to `backend/tests/test_simulation_service.py`, adding `from app.schemas.transaction import TransactionCreate` and `from app.services import transaction_service` to the imports:

```python
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
```

- [ ] **Step 2: Write the purity guard**

Cheap, and it pins the promise that simulating never writes:

```python
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
```

- [ ] **Step 3: Run both guards**

Run from `backend/`: `pytest tests/test_simulation_service.py -v -k "reality or never_writes"`

Expected: 2 passed. If the equivalence guard fails, the delta arithmetic in Task 1 disagrees with the real engine — fix the service, not the test.

- [ ] **Step 4: Run the whole file plus the neighbouring suites**

Run from `backend/`: `pytest tests/test_simulation_service.py tests/test_budget_service.py tests/test_transaction_calendar_service.py -v`

Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_simulation_service.py
git commit -m "test(simulation): guard delta arithmetic against the real engine"
```

---

### Task 5: Attach the impact to the MCP proposal

**Files:**
- Modify: `backend/mcp_server/tools/proposals.py` (the `propose_create_transaction` tool, preview built around `:558-562`)
- Test: `backend/tests/test_agents_mcp_tools.py`

**Interfaces:**
- Consumes: `simulate_transaction()` from Tasks 1–3. Inside the tool, `acc` is the resolved `Account`, `cat` the resolved `Category | None`, and `target_date` the parsed purchase date — all already in scope where `preview` is built.
- Produces: `preview["impact"]`, a `TransactionImpact` dumped to a plain dict, consumed by the frontend card in Task 6.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_agents_mcp_tools.py`, following the calling convention already used by the other `propose_create_transaction` tests in that file:

```python
async def test_propose_create_transaction_includes_impact(
    session: AsyncSession, ctx: CallContext, test_account, test_categories
):
    """The preview carries the impact block so the card can render numbers."""
    handler = REGISTRY["propose_create_transaction"].handler
    r = await handler(
        session=session, ctx=ctx,
        description="Pizza",
        amount=150.0,
        type="debit",
        account_id=str(test_account.id),
        category_id=str(test_categories[0].id),
    )

    assert r["kind"] == "create_transaction"
    assert "impact" in r
    assert "balance" in r["impact"]
    assert "today_after" in r["impact"]["balance"]
```

This mirrors `test_propose_create_transaction_full` in the same module: the
`ctx: CallContext` fixture, the handler pulled from `REGISTRY`, and keyword-only
invocation. `REGISTRY`, `CallContext` and `AsyncSession` are already imported at
the top of that file.

- [ ] **Step 2: Run the test to verify it fails**

Run from `backend/`: `pytest tests/test_agents_mcp_tools.py -v -k impact`

Expected: FAIL — `assert 'impact' in result`

- [ ] **Step 3: Attach the impact to the preview**

In `backend/mcp_server/tools/proposals.py`, replace the `preview` assignment in `propose_create_transaction`:

```python
    from app.services.simulation_service import simulate_transaction

    # Read-only, computed before any write path is considered. Every
    # proposal gets it: three reads inside a turn already waiting on the LLM.
    impact = await simulate_transaction(
        session,
        ws_id,
        ctx.user_id,
        amount=Decimal(str(amount)),
        currency=proposed["currency"],
        type=type,
        tx_date=target_date,
        account=acc,
        category=cat,
    )

    preview = {
        "kind": "create_transaction",
        "proposed": proposed,
        "impact": impact.model_dump(mode="json"),
        "apply_endpoint": "POST /api/transactions",
    }
```

Leave `_can_apply(ctx, apply)` and everything below it untouched — the write path must not change.

- [ ] **Step 4: Run the test to verify it passes**

Run from `backend/`: `pytest tests/test_agents_mcp_tools.py -v -k impact`

Expected: PASS

- [ ] **Step 5: Verify the write path did not regress**

Run from `backend/`: `pytest tests/test_agents_mcp_tools.py -v`

Expected: all pass, including every existing `apply=true` test. This is the path that writes to the database — a regression here is serious.

- [ ] **Step 6: Teach the LLM how to narrate it**

In the same file, extend `_PROPOSAL_PREFACE` (`:64-77`) by appending this sentence to the existing string, inside the closing parenthesis:

```python
    "When the response carries an `impact` block, mention it in ONE short "
    "sentence, leading with whatever broke (over budget, negative month end, "
    "over the card limit) or simply what is left. The card already shows the "
    "numbers - do NOT restate them in prose."
```

This is not decoration: the model reads these descriptions to decide what to say, and without the instruction it re-lists every figure in text.

- [ ] **Step 7: Run the agent test suites**

Run from `backend/`: `pytest tests/test_agents_mcp_tools.py tests/test_agents_executor.py -v`

Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add backend/mcp_server/tools/proposals.py backend/tests/test_agents_mcp_tools.py
git commit -m "feat(agents): show purchase impact in transaction proposals"
```

---

### Task 6: Render the impact on the proposal card

The locale keys ship **in this same task**. `frontend/src/locales/i18n.test.ts` compares keys across all 13 files, so adding them to `en.json` alone leaves the suite red — this is one deliverable, not two.

**Files:**
- Modify: `frontend/src/components/agents/proposal-card.tsx`
- Create: `frontend/src/components/agents/proposal-card.test.tsx`
- Modify: `frontend/src/locales/{de,en,es,fr,it,nl,pl,pt-BR,pt-PT,ru,sk,uk}.json` (13 files)

**Interfaces:**
- Consumes: `preview.impact` from Task 5, shaped as `TransactionImpact.model_dump(mode="json")` — `{ balance: {today_before, today_after, month_end_before, month_end_after, ends_month_negative, currency}, budget: {category_name, limit, spent_before, spent_after, remaining_after, exceeds_budget, currency} | null, credit_card: {bill_due_date, available_before, available_after, exceeds_credit_limit} | null }`.
- Consumes: `formatCurrency` from `frontend/src/lib/format.ts`.
- Produces: nothing other tasks depend on. This is the last task.

- [ ] **Step 1: Add the locale keys to all 13 files**

Under `agents.proposal`, add an `impact` object. English (`en.json`):

```json
"impact": {
  "budget": "{{category}} {{before}} → {{after}} of {{limit}}",
  "over": "over by {{amount}}",
  "monthEnd": "Month end {{before}} → {{after}}",
  "bill": "Bill due {{date}} · limit {{before}} → {{after}}",
  "overLimit": "over the card limit"
}
```

Brazilian Portuguese (`pt-BR.json`):

```json
"impact": {
  "budget": "{{category}} {{before}} → {{after}} de {{limit}}",
  "over": "estourou em {{amount}}",
  "monthEnd": "Fim do mês {{before}} → {{after}}",
  "bill": "Fatura em {{date}} · limite {{before}} → {{after}}",
  "overLimit": "acima do limite do cartão"
}
```

Translate the same five keys for `de`, `es`, `fr`, `it`, `nl`, `pl`, `pt-PT`, `ru`, `sk`, `uk`. **Every file must carry all five keys with the exact same placeholders** (`{{category}}`, `{{before}}`, `{{after}}`, `{{limit}}`, `{{amount}}`, `{{date}}`) — the parity test checks placeholders, not just key names.

- [ ] **Step 2: Verify locale parity before touching the component**

Run from `frontend/`: `npm run test -- i18n`

Expected: PASS. If it fails, a locale is missing a key or a placeholder — fix it now, while the cause is obvious.

- [ ] **Step 3: Write the failing component test**

Create `frontend/src/components/agents/proposal-card.test.tsx`. Use `renderWithProviders` from `frontend/src/test/utils.tsx` (not RTL's bare `render`) so the i18n and query-client providers are wired up. `ProposalCard` is a named export and requires both `toolCallId` and `data` props (`proposal-card.tsx:63`):

```tsx
import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderWithProviders } from '@/test/utils'
import { ProposalCard } from './proposal-card'

const baseData = {
  kind: 'create_transaction' as const,
  proposed: {
    description: 'Pizza',
    amount: 150,
    currency: 'BRL',
    type: 'debit',
    date: '2026-09-05',
    account_id: 'a1',
    account_name: 'Cartao',
    category_id: 'c1',
    category_name: 'Alimentacao',
  },
  apply_endpoint: 'POST /api/transactions',
}

const fullImpact = {
  balance: {
    today_before: 2000,
    today_after: 1850,
    month_end_before: 530,
    month_end_after: 380,
    ends_month_negative: false,
    currency: 'BRL',
  },
  budget: {
    category_name: 'Alimentacao',
    limit: 1800,
    spent_before: 1850,
    spent_after: 2000,
    remaining_after: -200,
    exceeds_budget: true,
    currency: 'BRL',
  },
  credit_card: {
    bill_due_date: '2026-10-10',
    available_before: 2300,
    available_after: 2150,
    exceeds_credit_limit: false,
  },
}

describe('ProposalCard impact', () => {
  it('renders all three lines when every block is present', () => {
    renderWithProviders(<ProposalCard toolCallId="t1" data={{ ...baseData, impact: fullImpact }} />)

    expect(screen.getByTestId('impact-budget')).toBeInTheDocument()
    expect(screen.getByTestId('impact-month-end')).toBeInTheDocument()
    expect(screen.getByTestId('impact-bill')).toBeInTheDocument()
  })

  it('hides the budget line when there is no budget', () => {
    renderWithProviders(
      <ProposalCard
        toolCallId="t2"
        data={{ ...baseData, impact: { ...fullImpact, budget: null } }}
      />,
    )

    expect(screen.queryByTestId('impact-budget')).not.toBeInTheDocument()
    expect(screen.getByTestId('impact-month-end')).toBeInTheDocument()
  })

  it('hides the bill line for a non-card account', () => {
    renderWithProviders(
      <ProposalCard
        toolCallId="t3"
        data={{ ...baseData, impact: { ...fullImpact, credit_card: null } }}
      />,
    )

    expect(screen.queryByTestId('impact-bill')).not.toBeInTheDocument()
  })

  it('renders exactly as before when there is no impact at all', () => {
    renderWithProviders(<ProposalCard toolCallId="t4" data={baseData} />)

    expect(screen.queryByTestId('impact-budget')).not.toBeInTheDocument()
    expect(screen.queryByTestId('impact-month-end')).not.toBeInTheDocument()
    expect(screen.queryByTestId('impact-bill')).not.toBeInTheDocument()
  })
})
```

`toolCallId` must differ per test: the card persists applied-state under the
`securo:agent-proposal-applied` localStorage key, keyed by that id.

- [ ] **Step 4: Run the test to verify it fails**

Run from `frontend/`: `npm run test -- proposal-card`

Expected: FAIL — `Unable to find an element by: [data-testid="impact-budget"]`

- [ ] **Step 5: Add the impact types**

In `frontend/src/components/agents/proposal-card.tsx`, above the existing `ProposalData` interface (`:28`), add:

```tsx
type BudgetImpact = {
  category_name: string
  limit: number
  spent_before: number
  spent_after: number
  remaining_after: number
  exceeds_budget: boolean
  currency: string
}

type BalanceImpact = {
  today_before: number
  today_after: number
  month_end_before: number
  month_end_after: number
  ends_month_negative: boolean
  currency: string
}

type CreditCardImpact = {
  bill_due_date: string
  available_before: number | null
  available_after: number | null
  exceeds_credit_limit: boolean
}

type Impact = {
  balance: BalanceImpact
  budget?: BudgetImpact | null
  credit_card?: CreditCardImpact | null
}
```

and add `impact?: Impact | null` to the `ProposalData` interface.

- [ ] **Step 6: Render the block**

Add this component in the same file:

```tsx
function ImpactLines({ impact }: { impact?: Impact | null }) {
  const { t } = useTranslation()
  if (!impact) return null

  const money = (v: number, currency: string) => formatCurrency(v, currency)

  return (
    <div className="mt-1.5 space-y-0.5 text-[13px] leading-snug">
      {impact.budget && (
        <div data-testid="impact-budget" className="text-muted-foreground">
          {t('agents.proposal.impact.budget', {
            category: impact.budget.category_name,
            before: money(impact.budget.spent_before, impact.budget.currency),
            after: money(impact.budget.spent_after, impact.budget.currency),
            limit: money(impact.budget.limit, impact.budget.currency),
          })}
          {impact.budget.exceeds_budget && (
            <span className="ml-1.5 text-amber-700 dark:text-amber-400">
              {t('agents.proposal.impact.over', {
                amount: money(
                  Math.abs(impact.budget.remaining_after),
                  impact.budget.currency,
                ),
              })}
            </span>
          )}
        </div>
      )}
      <div
        data-testid="impact-month-end"
        className={
          impact.balance.ends_month_negative
            ? 'text-red-600 dark:text-red-400'
            : 'text-muted-foreground'
        }
      >
        {t('agents.proposal.impact.monthEnd', {
          before: money(impact.balance.month_end_before, impact.balance.currency),
          after: money(impact.balance.month_end_after, impact.balance.currency),
        })}
      </div>
      {impact.credit_card && impact.credit_card.available_before !== null && (
        <div data-testid="impact-bill" className="text-muted-foreground">
          {t('agents.proposal.impact.bill', {
            date: impact.credit_card.bill_due_date,
            before: money(impact.credit_card.available_before, impact.balance.currency),
            after: money(impact.credit_card.available_after ?? 0, impact.balance.currency),
          })}
          {impact.credit_card.exceeds_credit_limit && (
            <span className="ml-1.5 text-amber-700 dark:text-amber-400">
              {t('agents.proposal.impact.overLimit')}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
```

Import `formatCurrency` from `@/lib/format` at the top of the file if it is not already imported.

- [ ] **Step 7: Mount it in the card**

In the card's JSX, immediately after the existing summary line:

```tsx
          <div className="mt-1 text-muted-foreground text-[13px] leading-snug">{summary}</div>
          <ImpactLines impact={data.impact} />
          <SplitPreview proposed={data.proposed} />
```

- [ ] **Step 8: Run the component test**

Run from `frontend/`: `npm run test -- proposal-card`

Expected: 4 passed

- [ ] **Step 9: Run typecheck, lint, and the whole frontend suite**

Run from `frontend/`:

```bash
npm run typecheck
npm run lint
npm run test
```

Expected: all clean. The i18n parity test in particular must pass.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/components/agents/proposal-card.tsx frontend/src/components/agents/proposal-card.test.tsx frontend/src/locales
git commit -m "feat(agents): render purchase impact on the proposal card"
```

---

## Self-Review

Checked after writing, before handoff.

**Spec coverage**

| Spec section | Task |
|---|---|
| `simulation_service` contract, pure read | 1 |
| Balance block, delta arithmetic, reference month | 1 |
| Budget block, honors global accounting mode | 2 |
| Credit card block, bill date and limit | 3 |
| Edge cases -> `None` (no budget, no category, income, non-card, no limit) | 2, 3 |
| Multi-currency conversion | 1 (service), exercised throughout |
| Equivalence and purity guards | 4 |
| MCP preview carries `impact`; write path unchanged | 5 |
| Tool description teaches one-sentence narration | 5 |
| Card renders three lines, each hidden with its block | 6 |
| 13 locales with matching placeholders | 6 |
| No endpoint, no migration, no other `propose_*` touched | enforced by Global Constraints |

**Type consistency** — `simulate_transaction` keeps the same keyword-only signature from Task 1 through Task 5. `TransactionImpact.balance` is non-optional everywhere; `budget` and `credit_card` are `Optional` in Python and `?: T | null` in TypeScript, matching `model_dump(mode="json")` output. `BudgetVsActual.budget_amount` / `.actual_amount` are used with the names the real schema defines.

**Known deviation from the spec's naming:** the spec's data-contract sketch listed `budget` before `balance`. The plan makes `balance` the first, non-optional field since it is always present. Behavior is identical.

**Multi-currency note:** Task 1 converts the amount to the primary currency and every block reports in it, so `BudgetImpact.currency` and `BalanceImpact.currency` are always the primary currency. The `credit_card` block reuses `balance.currency` for the same reason.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-05-purchase-simulation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — tasks executed in this session using executing-plans, batch execution with checkpoints

Which approach?
