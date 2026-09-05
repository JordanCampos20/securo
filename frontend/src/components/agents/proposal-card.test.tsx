import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { renderWithProviders } from '@/test/utils'
import { formatCurrency } from '@/lib/format'
import { ProposalCard } from './proposal-card'

// The display locale is independent of the UI language (see
// `useDisplayLocale`'s doc comment): a Brazilian user reading the English
// UI still expects "R$ 1.850,00", not "R$1,850.00". Pinning it to a locale
// that is neither the UI language ('en', from the real i18n bundle
// `renderWithProviders` wires up) nor `formatCurrency`'s 'en-US' default
// makes a regression to the default visible in the rendered text below.
vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'pt-BR',
}))

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
    // Deliberately different from balance.currency ('BRL') so the bill
    // line's assertion below fails if the component falls back to the
    // balance's currency instead of the card's own.
    currency: 'USD',
  },
}

describe('ProposalCard impact', () => {
  it('renders all three lines when every block is present', () => {
    renderWithProviders(<ProposalCard toolCallId="t1" data={{ ...baseData, impact: fullImpact }} />)

    expect(screen.getByTestId('impact-budget')).toBeInTheDocument()
    expect(screen.getByTestId('impact-month-end')).toBeInTheDocument()
    expect(screen.getByTestId('impact-bill')).toBeInTheDocument()
  })

  it('formats money for the mocked display locale, not the default', () => {
    renderWithProviders(<ProposalCard toolCallId="t5" data={{ ...baseData, impact: fullImpact }} />)

    // pt-BR formatting ("R$ 530,00"), not formatCurrency's 'en-US'
    // default ("R$530.00") — see the useDisplayLocale mock above.
    // `toHaveTextContent` normalizes the rendered DOM text (collapsing the
    // non-breaking space Intl.NumberFormat inserts after "R$"/"US$" into a
    // plain space) but does not normalize the string it is matched
    // against, so the expected values need the same normalization here.
    const plain = (s: string) => s.replace(/ /g, ' ')

    const monthEnd = screen.getByTestId('impact-month-end')
    expect(monthEnd).toHaveTextContent(plain(formatCurrency(530, 'BRL', 'pt-BR')))
    expect(monthEnd).toHaveTextContent(plain(formatCurrency(380, 'BRL', 'pt-BR')))
    expect(monthEnd.textContent).not.toContain(formatCurrency(530, 'BRL', 'en-US'))

    // The bill line must use the card's own currency (USD here), not the
    // balance's (BRL) — see the credit_card.currency comment above.
    const bill = screen.getByTestId('impact-bill')
    expect(bill).toHaveTextContent(plain(formatCurrency(2300, 'USD', 'pt-BR')))
    expect(bill).toHaveTextContent(plain(formatCurrency(2150, 'USD', 'pt-BR')))
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
