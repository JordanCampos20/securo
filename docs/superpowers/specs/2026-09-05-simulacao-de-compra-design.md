# Purchase simulation — design

Date: 2026-09-05 · Status: proposed

## Problem

Ad-hoc food spending accumulates invisibly. By the time the user notices,
the month is already blown. The question they want answered *before*
paying is: **"if I buy this pizza for R$150 on my credit card right now,
where does that leave me?"**

Securo can already project balances forward and track budgets, but nothing
in the system accepts a **hypothetical** transaction as input. Every
calculation reads exclusively from what is already persisted.

## Scope

This spec covers the **simulation** only. Voice input (audio -> transcription
-> agent chat) is a separate, orthogonal project with its own spec: it serves
every agent question, not just simulation, and it has nothing to answer until
this exists. Build order is simulation first, voice second.

## Decisions

| Question | Decision |
|---|---|
| What does the answer contain? | Both category budget **and** end-of-month balance |
| Yardstick for "am I tight?" | The category budget |
| Credit-card bucketing | Follow the global `credit_card_accounting_mode` setting, do not override it |
| Where it lives | Enriches the existing `propose_create_transaction` MCP tool |
| When it runs | On every proposal, not on demand |
| Persisted? | No. Read-only, no tables, no migration |

### Why enrich the existing proposal tool

A separate read-only `simulate_transaction` tool was considered and rejected.
It costs two LLM round-trips (simulate, then propose), which is slow and
invites drift — the model can simulate R$150 and then propose R$115. Folding
the impact into the proposal makes "simulate" and "book it" the same call:
one round-trip returns the numbers *and* the Apply button that already exists.
This also matters for the voice project later, where latency is the whole
game.

A REST endpoint plus a dedicated simulator screen was also rejected: it is the
opposite of the ask-and-answer flow the user described. If it is ever wanted,
`simulation_service` is already the right shape to expose.

## Architecture

One new service, `backend/app/services/simulation_service.py`, with a single
public function:

```python
async def simulate_transaction(
    session, workspace_id, user_id, *,
    amount: Decimal, currency: str, type: Literal["debit", "credit"],
    tx_date: date, account: Account, category: Category | None,
) -> TransactionImpact
```

It writes nothing, opens no transaction, persists no simulation. It takes an
already-validated hypothetical purchase, reads current state, returns the
impact. Pure read.

### Reused building blocks

Everything it needs already exists:

| Needs | Source |
|---|---|
| Balance today | `dashboard_service._balance_at()` |
| Projected end-of-month balance | `transaction_calendar_service.get_transaction_calendar()` |
| Category budget | `budget_service.get_budget_vs_actual()` |
| Which bill a purchase lands on | `credit_card_service.compute_effective_date()` |
| Available credit | `credit_card_service.compute_available_credit()` |

Two findings from reading those:

- `get_budget_vs_actual()` already filters `report_date <= today` and
  `status == "posted"`, so it answers *"how much have I committed so far this
  month"* directly, with recurring-budget resolution and FX included.
- `get_transaction_calendar()` returns `ending_balance` per day. The last
  `in_month` day **is** the projected end-of-month balance — the full cash-flow
  report is not needed.

### Boundary

The service returns **facts and flags**, never sentences. The LLM turns them
into prose; the card turns them into pixels. That keeps one calculation
serving today's chat, tomorrow's voice flow, and a REST endpoint if one is
ever added.

Only `propose_create_transaction` (`backend/mcp_server/tools/proposals.py`)
calls it. No new endpoint — YAGNI; it is ~10 lines the day it is needed.

## Data contract

`backend/app/schemas/simulation.py`. Three independent blocks, each either
fully populated or `None` — never half-filled.

```python
class BudgetImpact(BaseModel):
    category_name: str
    limit: float
    spent_before: float
    spent_after: float
    remaining_after: float      # negative when over
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
    bill_due_date: date
    available_before: float | None   # None when the card has no limit set
    available_after: float | None
    exceeds_credit_limit: bool

class TransactionImpact(BaseModel):
    budget: BudgetImpact | None
    balance: BalanceImpact
    credit_card: CreditCardImpact | None
```

`balance` is always present. `budget` and `credit_card` are absent whenever
they do not apply — see Edge cases.

## Calculation

**Core idea:** a hypothetical transaction does not require running the
projection twice. Balance is a running total by date, so a R$150 purchase
today shifts the whole curve by -150 from that date onward. Run the "before"
**once**, apply the delta arithmetically. Exact, and no doubled query cost.

```
delta = ±amount converted to the primary currency
        (debit negative, credit positive)
```

### Balance — shifts on the purchase date

`_balance_at()` and the calendar both accumulate by `Transaction.date`, never
`effective_date` (`dashboard_service.py:1231-1310`). That is correct: a card
purchase grows the card's debt on the day it is made.

| Field | Calculation |
|---|---|
| `today_before` | `_balance_at(today, include_pending=True)` |
| `today_after` | `today_before + delta` when `tx_date <= today`, else unchanged |
| `month_end_before` | last `in_month` calendar day -> `ending_balance` |
| `month_end_after` | `month_end_before + delta` (always shifts; see reference month) |

A future-dated purchase ("what if I buy on Saturday?") leaves today untouched
and moves only the month-end figure. That falls out of the rule.

**Reference month.** The calendar is always built for the month of `tx_date`,
not the current month. So a purchase dated next month reports *that* month's
projected end balance, and `month_end_after` always shifts by the delta.
`today_before` / `today_after` are always about today, which is why only they
carry the `tx_date <= today` condition.

### Budget — bucketed by the global setting

```
spent_after     = spent_before + amount_primary
remaining_after = limit - spent_after
exceeds_budget  = spent_after > limit
```

The month is chosen by the same `reporting_date_col()` the rest of the app
uses, honoring the global `credit_card_accounting_mode`. **The simulation does
not impose its own yardstick.** If it did, its number would disagree with the
Budgets screen and the user would not know which to trust.

Note the naming is inverted from standard accounting terms
(`_query_filters.py:89-92`): mode **`cash`** buckets by `Transaction.date`
(purchase month) and **`accrual`** buckets by `effective_date` (bill month).
`cash` is the default (`admin_service.py:255`), so the desired behavior —
the pizza counts as spending today — ships without configuration.

### Credit card — context, not arithmetic

Populated only when the account is a card. Carries `bill_due_date` and
available credit before/after. It does not feed the two headline numbers; it
answers *"it lands on the Oct 10 bill, and your limit goes from R$2,300 to
R$2,150."*

### Edge cases — all resolved by omission

- Category has no budget, or no category given -> `budget: None`; balance still
  answers. No implicit yardstick is invented.
- Income (`type == "credit"`) -> positive delta, `budget` always `None`
  (budgets track expenses only).
- Non-card account -> `credit_card: None`.
- Card with no `credit_limit` set -> `available_before` / `available_after` are
  `None` and `exceeds_credit_limit` is `False`; the block still carries
  `bill_due_date`.
- Foreign currency -> converted via `fx_rate_service.convert()`, like the rest
  of the app.

## Integration

### Backend — `mcp_server/tools/proposals.py`

`propose_create_transaction` already resolves account and category before
building its preview (`:470-560`). Immediately after, it calls
`simulation_service` and attaches the result:

```python
preview = {
    "kind": "create_transaction",
    "proposed": proposed,
    "impact": impact,            # new
    "apply_endpoint": "POST /api/transactions",
}
```

Nothing else changes. `_can_apply()` and the entire write path stay untouched —
`impact` is pure read, computed before and independent of any write.

Impact is computed **always**, not on demand. Cost is three reads (balance,
one month of calendar, budgets), inside a chat turn already waiting on the
LLM. An `include_impact` flag would be configuration to save what does not
hurt.

**The tool description matters as much as the code.** `_PROPOSAL_PREFACE`
(`:64-77`) exists precisely because the LLM reads these descriptions to decide
what to say. It must instruct: *narrate the impact in one sentence, leading
with whatever broke; do not restate the numbers already on the card.* Without
that the model re-lists everything in prose — unbearable over voice later.

### Frontend — `proposal-card.tsx`

The card already renders by `kind` and already maps `create_transaction` ->
`POST /api/transactions` on Apply (`:302-338`). Add an impact block between
the summary and the buttons, shown only when `data.impact` exists:

```
Alimentação   R$ 1,850 -> R$ 2,000   of R$ 1,800   ⚠ over by R$ 200
Month end     R$ 530 -> R$ 380
Bill          due Oct 10 · limit R$ 2,300 -> R$ 2,150
```

Each line disappears when its block is `None`. With no budget configured the
card shows balance only — no empty space, no em-dash placeholders.

`frontend/src/locales/i18n.test.ts` compares keys across all 13 locales and
validates placeholders, so **new keys must be translated in all 13**. This is
the real cost of the frontend change.

## Out of scope

- No new REST endpoint.
- No table, no migration — simulations are never persisted.
- No changes to the other `propose_*` tools. Only transactions gain impact.
- No "suggest creating a budget" when none exists. `propose_create_budget`
  already exists and the agent uses it when asked.
- Voice input. Separate project.

## Testing

TDD — tests before implementation. Backend uses the existing suite
(`pytest-asyncio`, `session` / `test_user` / `test_workspace` fixtures from
`conftest.py`).

### `backend/tests/test_simulation_service.py` (new)

**Balance**
- Debit today on a checking account -> both today and month-end drop by the amount
- Purchase dated 3 days out -> today unchanged, month-end drops
- Purchase dated next month -> today unchanged; month-end reports **next** month's
  projection and drops by the amount; budget lands in the right month

**Budget**
- `cash` mode (default): card purchase counts in the **purchase** month
- `accrual` mode: the same purchase counts in the **bill** month — this is the
  test that pins the promise of agreeing with the Budgets screen
- `exceeds_budget` flips on the exact cent that crosses, not before

**Blocks that become `None`**
- Category without a budget -> `budget is None`, `balance` populated
- Transaction without a category -> `budget is None`
- Income (`credit`) -> positive delta, `budget is None`
- Non-card account -> `credit_card is None`

**Multi-currency** — a USD purchase with BRL as primary is converted in both blocks.

**The highest-value test — simulation/reality equivalence**

> Simulate a purchase. Record `today_after` and `month_end_after`. **Create the
> transaction for real.** Read the actual balance and calendar. The numbers must
> match.

This proves the arithmetic delta shortcut does not lie. If someone changes the
projection engine later, this test breaks — and it is the thing that should
break, not the user discovering it at month end.

**Purity test** — count transactions before and after simulating; must be
equal. Cheap, and it pins the promise that simulating never writes.

### `backend/tests/test_agents_mcp_tools.py` (existing)

`propose_create_transaction` returns `impact`, and the `apply=true` path stays
identical. A regression here would be serious — that is the path that writes.

### `frontend/src/components/agents/proposal-card.test.tsx` (new)

All three lines render with full data; each disappears when its block is
`None`; a card with no `impact` renders exactly as before.

### `frontend/src/locales/i18n.test.ts` (existing)

Passes for free once all 13 translations land. It is the safety net that
catches a forgotten locale.

## Risks

- **Projection quality depends on the user's data.** Month-end balance is only
  as good as the recurring transactions configured. Mitigated by
  `_get_baseline_projection()`, which already falls back to an adaptive
  historical average when no recurrings exist — so the feature degrades rather
  than breaking.
- **The budget block is silent when no budget exists**, which is the state most
  users are in. The feature still answers the balance question, but the
  yardstick the user chose is unavailable until they set a budget for the
  category.
- **13 translations** is mechanical work that the parity test will not let us
  skip.

## Next step

Implementation plan via the writing-plans skill.
