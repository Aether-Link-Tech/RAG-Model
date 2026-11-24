import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from '../App';
import { ToastProvider } from '../components/Toast';
import { session } from '../services/session';

vi.mock('../services/publicService', async (orig) => ({
  ...(await orig<typeof import('../services/publicService')>()),
  listShowcase: vi.fn(),
  askPublic: vi.fn(),
}));

import * as pub from '../services/publicService';

function renderAt(path: string) {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </ToastProvider>,
  );
}

beforeEach(() => {
  session.clear();
  vi.mocked(pub.listShowcase).mockResolvedValue(['library.txt']);
});

describe('landing page', () => {
  it('is public and explains the project with sign-in links', async () => {
    renderAt('/');
    expect(screen.getByRole('heading', { name: /ask your documents/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /^sign in$/i })).toHaveAttribute('href', '/login');
    expect(screen.getByText('Muhammad Ayyan')).toBeInTheDocument();
    expect(await screen.findByText('library.txt')).toBeInTheDocument();
  });

  it('answers a guest question with cited, highlighted excerpts and no download', async () => {
    vi.mocked(pub.askPublic).mockResolvedValue({
      answer: 'The library opens at 10 am on weekends [1].',
      sources: [{
        marker: 1, document_name: 'library.txt', page: 2, section: null, source_type: 'text', score: 0.8,
        text: '…closes at 10 pm. On weekends the library opens at 10 am. Students must…',
        highlight: 'On weekends the library opens at 10 am.',
      }],
      insufficient: false, uncertain: false, uncertain_reason: null,
    });
    renderAt('/');
    await screen.findByText('library.txt');
    await userEvent.type(screen.getByLabelText(/ask a question/i), 'When does the library open on weekends?');
    await userEvent.click(screen.getByRole('button', { name: /^ask/i }));
    const answer = await screen.findByTestId('public-answer');
    expect(within(answer).getByRole('button', { name: 'Source 1' })).toBeInTheDocument();
    expect(answer.querySelector('mark')).toHaveTextContent('On weekends the library opens at 10 am.');
    expect(within(answer).queryByRole('button', { name: /download/i })).not.toBeInTheDocument();
    expect(within(answer).getByText(/cannot be downloaded/i)).toBeInTheDocument();
  });

  it('explains the rate limit', async () => {
    vi.mocked(pub.askPublic).mockRejectedValue(
      Object.assign(new Error('x'), { isAxiosError: true, response: { status: 429, data: { detail: 'Too many' } } }),
    );
    renderAt('/');
    await screen.findByText('library.txt');
    await userEvent.type(screen.getByLabelText(/ask a question/i), 'hours?');
    await userEvent.click(screen.getByRole('button', { name: /^ask/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/sign in to keep asking/i);
  });
});
