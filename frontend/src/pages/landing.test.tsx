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
  getFreeQuestions: vi.fn(),
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
  vi.mocked(pub.getFreeQuestions).mockResolvedValue({ free_questions_left: 5, free_questions_limit: 5 });
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
      free_questions_left: 4, free_questions_limit: 5,
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
    expect(screen.getByTestId('free-questions')).toHaveTextContent('4 of 5 free questions left today');
  });

  it('asks guests to sign in when the free questions are used up', async () => {
    vi.mocked(pub.getFreeQuestions).mockResolvedValue({ free_questions_left: 0, free_questions_limit: 5 });
    renderAt('/');
    const prompt = await screen.findByTestId('sign-in-prompt');
    expect(within(prompt).getByRole('link', { name: /create account/i })).toHaveAttribute('href', '/signup');
    expect(screen.getByLabelText(/ask a question/i)).toBeDisabled();
  });

  it('shows the sign-in prompt when the server says the free limit is reached', async () => {
    vi.mocked(pub.askPublic).mockRejectedValue(
      Object.assign(new Error('x'), {
        isAxiosError: true,
        response: { status: 429, data: { detail: 'You have used your 5 free questions for today. Sign in or create a free account to keep asking.' } },
      }),
    );
    renderAt('/');
    await screen.findByText('library.txt');
    await userEvent.type(screen.getByLabelText(/ask a question/i), 'hours?');
    await userEvent.click(screen.getByRole('button', { name: /^ask/i }));
    expect(await screen.findByTestId('sign-in-prompt')).toBeInTheDocument();
  });

  it('explains the burst limit', async () => {
    vi.mocked(pub.askPublic).mockRejectedValue(
      Object.assign(new Error('x'), { isAxiosError: true, response: { status: 429, data: { detail: 'You are asking very quickly. Please wait a moment, or sign in to keep asking.' } } }),
    );
    renderAt('/');
    await screen.findByText('library.txt');
    await userEvent.type(screen.getByLabelText(/ask a question/i), 'hours?');
    await userEvent.click(screen.getByRole('button', { name: /^ask/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/sign in to keep asking/i);
  });
});
