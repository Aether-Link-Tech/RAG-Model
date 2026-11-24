import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from '../App';
import { ToastProvider } from '../components/Toast';
import { session } from '../services/session';
import type { ChatAnswer } from '../types';

vi.mock('../services/chatService', () => ({
  ask: vi.fn(),
  ChatError: class ChatError extends Error {},
}));
vi.mock('../services/documentService', () => ({ listDocuments: vi.fn(async () => []) }));

import * as chat from '../services/chatService';

const answer = (over: Partial<ChatAnswer> = {}): ChatAnswer => ({
  conversation_id: 'conv-1', message_id: 'm', answer: 'Leave is 20 days [1].',
  sources: [{ marker: 1, document_id: 'd1', document_name: 'Handbook.pdf', page: 4, section: 'Leave', source_type: 'text', text: 'Employees get 20 days.', score: 0.88 }],
  insufficient: false, uncertain: false, uncertain_reason: null, claims: [], removed_claims: 0,
  injection_detected: false, comparison: null, ...over,
});

function renderChat() {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/chat']}>
        <AppRoutes />
      </MemoryRouter>
    </ToastProvider>,
  );
}

beforeEach(() => {
  vi.mocked(chat.ask).mockReset();
  session.start('tok', { id: 'me', email: 'me@x.io', full_name: 'Sara Khan', role: 'user', created_at: '', is_deleted: false }, 3600);
});

describe('chat page', () => {
  it('is reachable from the app bar', async () => {
    renderChat();
    expect(await screen.findByRole('link', { name: /ask/i })).toHaveAttribute('href', '/chat');
    expect(screen.getByRole('heading', { name: /hi sara/i })).toBeInTheDocument();
  });

  it('streams the answer, shows citations and keeps the conversation id for follow-ups', async () => {
    vi.mocked(chat.ask).mockImplementation(async (_req, handlers) => {
      handlers?.onToken?.('Leave is ');
      handlers?.onToken?.('20 days [1].');
      return answer();
    });
    renderChat();
    await userEvent.type(screen.getByLabelText(/your question/i), 'How much leave?{Enter}');

    expect(await screen.findByText('How much leave?')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /show source 1/i })).toBeInTheDocument();
    expect(screen.getByText('Handbook.pdf')).toBeInTheDocument();
    expect(vi.mocked(chat.ask).mock.calls[0][0]).toEqual({
      question: 'How much leave?', conversation_id: null, include_private: true, include_public: true,
    });

    vi.mocked(chat.ask).mockResolvedValue(answer({ answer: 'Yes.', sources: [] }));
    await userEvent.type(screen.getByLabelText(/your question/i), 'Is it paid?{Enter}');
    await waitFor(() => expect(vi.mocked(chat.ask).mock.calls[1][0].conversation_id).toBe('conv-1'));
  });

  it('respects the search scope toggles', async () => {
    vi.mocked(chat.ask).mockResolvedValue(answer());
    renderChat();
    await userEvent.click(screen.getByLabelText(/shared documents/i));
    await userEvent.type(screen.getByLabelText(/your question/i), 'q{Enter}');
    await waitFor(() => expect(vi.mocked(chat.ask).mock.calls[0][0]).toMatchObject({ include_private: true, include_public: false }));

    await userEvent.click(screen.getByLabelText(/my private files/i));
    expect(screen.getByText(/pick at least one place/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /send question/i })).toBeDisabled();
  });

  it('explains when the documents cannot answer', async () => {
    vi.mocked(chat.ask).mockResolvedValue(answer({ answer: "I couldn't find this.", sources: [], insufficient: true }));
    renderChat();
    await userEvent.type(screen.getByLabelText(/your question/i), 'Unknown?{Enter}');
    expect(await screen.findByText(/don't contain enough information/i)).toBeInTheDocument();
  });

  it('shows errors with a retry', async () => {
    vi.mocked(chat.ask).mockRejectedValueOnce(new chat.ChatError('The assistant is unavailable right now.'));
    vi.mocked(chat.ask).mockResolvedValueOnce(answer());
    renderChat();
    await userEvent.type(screen.getByLabelText(/your question/i), 'Hello{Enter}');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/unavailable/i);
    await userEvent.click(within(alert).getByRole('button', { name: /try again/i }));
    expect(await screen.findByText('Handbook.pdf')).toBeInTheDocument();
    expect(screen.getAllByText('Hello')).toHaveLength(1);
  });
});
