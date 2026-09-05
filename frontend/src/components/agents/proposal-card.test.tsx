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
