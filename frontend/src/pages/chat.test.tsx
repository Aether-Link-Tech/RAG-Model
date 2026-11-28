import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from '../App';
import { ToastProvider } from '../components/Toast';
import { session } from '../services/session';
import type { ChatAnswer, ConversationDetail, ConversationSummary, Document } from '../types';

vi.mock('../services/chatService', () => ({
  ask: vi.fn(),
  listConversations: vi.fn(async () => []),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(async () => undefined),
  ChatError: class ChatError extends Error {},
}));
vi.mock('../services/documentService', () => ({ listDocuments: vi.fn(async () => []) }));
vi.mock('../services/collectionService', () => ({ listCollections: vi.fn(async () => []) }));

import * as chat from '../services/chatService';
import * as docs from '../services/documentService';

const answer = (over: Partial<ChatAnswer> = {}): ChatAnswer => ({
  conversation_id: 'conv-1',
  message_id: 'm',
  answer: 'Leave is 20 days [1].',
  sources: [
    {
      marker: 1,
      document_id: 'd1',
      document_name: 'Handbook.pdf',
      page: 4,
      section: 'Leave',
      source_type: 'text',
      text: 'Employees get 20 days.',
      highlight: 'Employees get 20 days.',
      score: 0.88,
    },
  ],
  insufficient: false,
  uncertain: false,
  uncertain_reason: null,
  claims: [],
  removed_claims: 0,
  injection_detected: false,
  comparison: null,
  ...over,
});

const doc = (over: Partial<Document>): Document => ({
  id: 'd1',
  filename: 'Password Policy.txt',
  mime_type: 'text/plain',
  file_size: 1024,
  file_hash: 'a'.repeat(64),
  status: 'indexed',
  visibility: 'private',
  is_owner: true,
  user_id: 'me',
  error_message: null,
  created_at: new Date().toISOString(),
  updated_at: null,
  ...over,
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
  vi.mocked(chat.listConversations).mockReset();
  vi.mocked(chat.listConversations).mockResolvedValue([]);
  vi.mocked(chat.getConversation).mockReset();
  vi.mocked(docs.listDocuments).mockResolvedValue([]);
  session.start('tok', { id: 'me', email: 'me@x.io', full_name: 'Sara Khan', role: 'user', created_at: '', is_deleted: false }, 3600);
});

describe('chat page', () => {
  it('is reachable from the app bar and shows an upload action when no documents exist', async () => {
    renderChat();
    expect(await screen.findByRole('link', { name: /ask/i })).toHaveAttribute('href', '/chat');
    expect(screen.getByRole('heading', { name: /ask your documents/i })).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: /no documents loaded/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /upload documents/i })).toHaveAttribute('href', '/documents');
  });

  it('shows 3 suggested questions derived from real loaded documents', async () => {
    vi.mocked(docs.listDocuments).mockResolvedValue([
      doc({ id: '1', filename: 'Password Policy.txt' }),
      doc({ id: '2', filename: 'Remote Work Policy.txt' }),
      doc({ id: '3', filename: 'Annual Leave Policy.txt' }),
    ]);
    renderChat();
    const list = await screen.findByLabelText(/suggested questions/i);
    const buttons = within(list).getAllByRole('button');
    expect(buttons).toHaveLength(3);
    expect(buttons[0]).toHaveTextContent(/password/i);
  });

  it('streams the answer, shows citations and keeps the conversation id for follow-ups', async () => {
    vi.mocked(chat.ask).mockImplementation(async (_req, handlers) => {
      handlers?.onToken?.('Leave is ');
      handlers?.onToken?.('20 days [1].');
      return answer();
    });
    renderChat();
    await userEvent.type(screen.getByLabelText(/your question/i), 'How much leave?{Enter}');

    const thread = screen.getByTestId('chat-thread');
    expect(await within(thread).findByText('How much leave?')).toBeInTheDocument();
    const citeBtn = await screen.findByRole('button', { name: /show source 1/i });
    expect(screen.getByText('Handbook.pdf')).toBeInTheDocument();

    await userEvent.click(citeBtn);
    const sourcesPanel = screen.getByRole('complementary', { name: /retrieved sources/i });
    expect(sourcesPanel.querySelector('.source-card.is-highlighted')).toBeInTheDocument();

    expect(vi.mocked(chat.ask).mock.calls[0][0]).toEqual({
      question: 'How much leave?',
      conversation_id: null,
      include_private: true,
      include_public: true,
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
    expect(within(screen.getByTestId('chat-thread')).getAllByText('Hello')).toHaveLength(1);
  });

  it('renders conversational greetings without insufficient fallback notices and keeps conversation continuity', async () => {
    vi.mocked(chat.ask).mockImplementationOnce(async (_req, handlers) => {
      handlers?.onToken?.('Hi there! ');
      handlers?.onToken?.('How can I help you today?');
      return answer({
        conversation_id: 'conv-greet',
        answer: 'Hi there! How can I help you today?',
        sources: [],
        insufficient: false,
      });
    });

    renderChat();
    await userEvent.type(screen.getByLabelText(/your question/i), 'hi{Enter}');

    const thread = screen.getByTestId('chat-thread');
    expect(await within(thread).findByText('Hi there! How can I help you today?')).toBeInTheDocument();
    expect(screen.queryByText(/don't contain enough information/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no matching passages/i)).not.toBeInTheDocument();

    vi.mocked(chat.ask).mockResolvedValueOnce(answer({ conversation_id: 'conv-greet' }));
    await userEvent.type(screen.getByLabelText(/your question/i), 'How much leave?{Enter}');
    await waitFor(() => expect(vi.mocked(chat.ask).mock.calls[1][0].conversation_id).toBe('conv-greet'));
    expect(await within(thread).findByText(/Leave is/)).toBeInTheDocument();
  });

  it('does not overwrite a completed response with late tokens or stale aborted requests', async () => {
    let strayToken: ((text: string) => void) | undefined;
    vi.mocked(chat.ask).mockImplementationOnce(async (_req, handlers) => {
      strayToken = handlers?.onToken;
      handlers?.onToken?.('Authoritative ');
      handlers?.onToken?.('greeting.');
      return answer({
        conversation_id: 'conv-1',
        answer: 'Authoritative greeting.',
        sources: [],
        insufficient: false,
      });
    });

    renderChat();
    await userEvent.type(screen.getByLabelText(/your question/i), 'hello{Enter}');

    const thread = screen.getByTestId('chat-thread');
    expect(await within(thread).findByText('Authoritative greeting.')).toBeInTheDocument();

    // A late token callback firing after completion must never mutate the finalized answer.
    strayToken?.(' STALE_APPEND');
    expect(within(thread).getByText('Authoritative greeting.')).toBeInTheDocument();
    expect(within(thread).queryByText(/STALE_APPEND/)).not.toBeInTheDocument();
  });

  it('ignores stale completion when starting a new conversation mid-stream', async () => {
    let finishStale: ((val: ChatAnswer) => void) | undefined;
    vi.mocked(chat.ask).mockImplementationOnce(
      (_req, handlers) =>
        new Promise<ChatAnswer>((resolve) => {
          handlers?.onToken?.('Streaming old turn…');
          finishStale = resolve;
        }),
    );

    renderChat();
    await userEvent.type(screen.getByLabelText(/your question/i), 'First question{Enter}');
    expect(await screen.findByText('Streaming old turn…')).toBeInTheDocument();

    await userEvent.click(screen.getAllByRole('button', { name: /new conversation/i })[0]);

    // Even if the old promise resolves late after abort, it must not overwrite state.
    finishStale?.(
      answer({
        answer: 'The uploaded documents do not contain enough information to answer this question.',
        sources: [],
        insufficient: true,
      }),
    );

    vi.mocked(chat.ask).mockResolvedValueOnce(
      answer({ conversation_id: 'conv-2', answer: 'Good morning! How can I help?', sources: [], insufficient: false }),
    );
    await userEvent.type(screen.getByLabelText(/your question/i), 'good morning{Enter}');
    expect(await screen.findByText('Good morning! How can I help?')).toBeInTheDocument();
    expect(screen.queryByText(/do not contain enough information/i)).not.toBeInTheDocument();
  });

  it('shows LatticeLoader status across working, done, and error states', async () => {
    let resolveAnswer!: (val: ChatAnswer) => void;
    vi.mocked(chat.ask).mockImplementationOnce(
      () =>
        new Promise<ChatAnswer>((resolve) => {
          resolveAnswer = resolve;
        }),
    );

    renderChat();
    await userEvent.type(screen.getByLabelText(/your question/i), 'Status check{Enter}');

    const thread = screen.getByTestId('chat-thread');
    const loader = await within(thread).findByRole('status');
    expect(loader).toHaveAttribute('data-status', 'working');
    expect(loader).toHaveTextContent(/thinking/i);

    resolveAnswer(answer({ answer: 'Verified result.' }));
    await waitFor(() => expect(loader).toHaveAttribute('data-status', 'done'));
    expect(loader).toHaveTextContent(/done in/i);

    vi.mocked(chat.ask).mockRejectedValueOnce(new chat.ChatError('Network failure'));
    await userEvent.type(screen.getByLabelText(/your question/i), 'Error check{Enter}');
    await waitFor(() => expect(within(thread).getAllByRole('status')[1]).toHaveAttribute('data-status', 'error'));
    expect(within(thread).getAllByRole('status')[1]).toHaveTextContent(/failed after/i);
  });

  it('loads persisted conversations on mount, restores messages & citations, and switches threads cleanly', async () => {
    const summaries: ConversationSummary[] = [
      {
        id: 'conv-a',
        title: 'Annual leave policy',
        created_at: '2026-11-26T08:00:00Z',
        updated_at: '2026-11-26T08:05:00Z',
        message_count: 2,
      },
      {
        id: 'conv-b',
        title: 'Password length rules',
        created_at: '2026-11-26T07:00:00Z',
        updated_at: '2026-11-26T07:05:00Z',
        message_count: 2,
      },
    ];
    const detailA: ConversationDetail = {
      ...summaries[0],
      messages: [
        {
          id: 'u-a1',
          conversation_id: 'conv-a',
          role: 'user',
          content: 'Annual leave policy',
          sources: [],
          insufficient: false,
          uncertain: false,
          uncertain_reason: null,
          claims: [],
          removed_claims: 0,
          injection_detected: false,
          comparison: null,
          created_at: '2026-11-26T08:00:00Z',
        },
        {
          id: 'a-a1',
          conversation_id: 'conv-a',
          role: 'assistant',
          content: 'Leave is 20 days [1].',
          sources: answer().sources,
          insufficient: false,
          uncertain: false,
          uncertain_reason: null,
          claims: [],
          removed_claims: 0,
          injection_detected: false,
          comparison: null,
          created_at: '2026-11-26T08:00:01Z',
        },
      ],
    };
    const detailB: ConversationDetail = {
      ...summaries[1],
      messages: [
        {
          id: 'u-b1',
          conversation_id: 'conv-b',
          role: 'user',
          content: 'Password length rules',
          sources: [],
          insufficient: false,
          uncertain: false,
          uncertain_reason: null,
          claims: [],
          removed_claims: 0,
          injection_detected: false,
          comparison: null,
          created_at: '2026-11-26T07:00:00Z',
        },
        {
          id: 'a-b1',
          conversation_id: 'conv-b',
          role: 'assistant',
          content: 'Passwords must be 14 characters.',
          sources: [],
          insufficient: false,
          uncertain: false,
          uncertain_reason: null,
          claims: [],
          removed_claims: 0,
          injection_detected: false,
          comparison: null,
          created_at: '2026-11-26T07:00:01Z',
        },
      ],
    };

    vi.mocked(chat.listConversations).mockResolvedValue(summaries);
    vi.mocked(chat.getConversation).mockImplementation(async (id) => (id === 'conv-a' ? detailA : detailB));

    renderChat();

    const sidebar = screen.getByRole('complementary', { name: /conversations and documents/i });
    expect(await within(sidebar).findByText('Annual leave policy')).toBeInTheDocument();
    expect(within(sidebar).getByText('Password length rules')).toBeInTheDocument();

    // Select Conversation A: restores turns and interactive citations.
    await userEvent.click(within(sidebar).getByRole('button', { name: /annual leave policy/i }));
    const thread = screen.getByTestId('chat-thread');
    expect(await within(thread).findByText(/Leave is/)).toBeInTheDocument();
    const citeBtn = await screen.findByRole('button', { name: /show source 1/i });
    await userEvent.click(citeBtn);
    const sourcesPanel = screen.getByRole('complementary', { name: /retrieved sources/i });
    expect(sourcesPanel.querySelector('.source-card.is-highlighted')).toBeInTheDocument();

    // Switch to Conversation B: messages from A do not mix into B.
    await userEvent.click(within(sidebar).getByRole('button', { name: /password length rules/i }));
    expect(await within(thread).findByText('Passwords must be 14 characters.')).toBeInTheDocument();
    expect(within(thread).queryByText(/Leave is/)).not.toBeInTheDocument();

    // Switch back to Conversation A: cached turns render immediately without duplicate getConversation call.
    await userEvent.click(within(sidebar).getByRole('button', { name: /annual leave policy/i }));
    expect(await within(thread).findByText(/Leave is/)).toBeInTheDocument();
    expect(vi.mocked(chat.getConversation)).toHaveBeenCalledTimes(2);
  });

  it('restores conversations across route navigation (/documents -> /chat) and page remount', async () => {
    const persisted: ConversationSummary[] = [
      {
        id: 'conv-persisted',
        title: 'Incident reporting window',
        created_at: '2026-11-26T09:00:00Z',
        updated_at: '2026-11-26T09:01:00Z',
        message_count: 2,
      },
    ];
    vi.mocked(chat.listConversations).mockResolvedValue(persisted);
    vi.mocked(chat.getConversation).mockResolvedValue({
      ...persisted[0],
      messages: [
        {
          id: 'u1',
          conversation_id: 'conv-persisted',
          role: 'user',
          content: 'Incident reporting window',
          sources: [],
          insufficient: false,
          uncertain: false,
          uncertain_reason: null,
          claims: [],
          removed_claims: 0,
          injection_detected: false,
          comparison: null,
          created_at: '2026-11-26T09:00:00Z',
        },
        {
          id: 'a1',
          conversation_id: 'conv-persisted',
          role: 'assistant',
          content: 'Report within 24 hours.',
          sources: [],
          insufficient: false,
          uncertain: false,
          uncertain_reason: null,
          claims: [],
          removed_claims: 0,
          injection_detected: false,
          comparison: null,
          created_at: '2026-11-26T09:00:01Z',
        },
      ],
    });

    const view = renderChat();
    expect(await screen.findByText('Incident reporting window')).toBeInTheDocument();

    // Navigate away to /documents via the top navigation bar, then back to /chat.
    await userEvent.click(screen.getByRole('link', { name: /^documents$/i }));
    await userEvent.click(await screen.findByRole('link', { name: /^ask$/i }));
    expect(await screen.findByText('Incident reporting window')).toBeInTheDocument();

    // Simulate full browser refresh by unmounting and remounting the app.
    view.unmount();
    renderChat();
    const convBtn = await screen.findByRole('button', { name: /incident reporting window/i });
    await userEvent.click(convBtn);
    expect(await screen.findByText('Report within 24 hours.')).toBeInTheDocument();
  });

  it('surfaces sidebar and history API errors with retry instead of pretending no conversations exist', async () => {
    vi.mocked(chat.listConversations)
      .mockRejectedValueOnce(new chat.ChatError('Conversation history is unavailable right now.'))
      .mockResolvedValueOnce([
        {
          id: 'conv-err',
          title: 'Recovered conversation',
          created_at: '2026-11-26T09:00:00Z',
          updated_at: '2026-11-26T09:00:00Z',
          message_count: 2,
        },
      ]);

    renderChat();

    const sidebar = screen.getByRole('complementary', { name: /conversations and documents/i });
    const sidebarAlert = await within(sidebar).findByRole('alert');
    expect(sidebarAlert).toHaveTextContent(/unavailable/i);
    expect(within(sidebar).queryByText(/no conversations yet/i)).not.toBeInTheDocument();

    await userEvent.click(within(sidebarAlert).getByRole('button', { name: /retry/i }));
    const recoveredBtn = await within(sidebar).findByRole('button', { name: /recovered conversation/i });

    // Now fail getConversation once, then succeed on retry.
    vi.mocked(chat.getConversation)
      .mockRejectedValueOnce(new chat.ChatError('Failed to fetch messages.'))
      .mockResolvedValueOnce({
        id: 'conv-err',
        title: 'Recovered conversation',
        created_at: '2026-11-26T09:00:00Z',
        updated_at: '2026-11-26T09:00:00Z',
        message_count: 2,
        messages: [
          {
            id: 'u1',
            conversation_id: 'conv-err',
            role: 'user',
            content: 'Recovered conversation',
            sources: [],
            insufficient: false,
            uncertain: false,
            uncertain_reason: null,
            claims: [],
            removed_claims: 0,
            injection_detected: false,
            comparison: null,
            created_at: '2026-11-26T09:00:00Z',
          },
          {
            id: 'a1',
            conversation_id: 'conv-err',
            role: 'assistant',
            content: 'Restored after retry.',
            sources: [],
            insufficient: false,
            uncertain: false,
            uncertain_reason: null,
            claims: [],
            removed_claims: 0,
            injection_detected: false,
            comparison: null,
            created_at: '2026-11-26T09:00:01Z',
          },
        ],
      });

    await userEvent.click(recoveredBtn);
    const threadAlert = await within(screen.getByTestId('chat-thread')).findByRole('alert');
    expect(threadAlert).toHaveTextContent(/failed to fetch messages/i);

    await userEvent.click(within(threadAlert).getByRole('button', { name: /try again/i }));
    expect(await screen.findByText('Restored after retry.')).toBeInTheDocument();
  });

  it('isolates conversations across logout and user switches without storing history in web storage', async () => {
    vi.mocked(chat.listConversations).mockImplementation(async () =>
      session.user?.id === 'me'
        ? [
            {
              id: 'conv-sara',
              title: 'Sara private payroll notes',
              created_at: '2026-11-26T09:00:00Z',
              updated_at: '2026-11-26T09:00:00Z',
              message_count: 2,
            },
          ]
        : [
            {
              id: 'conv-omar',
              title: 'Omar onboarding question',
              created_at: '2026-11-26T10:00:00Z',
              updated_at: '2026-11-26T10:00:00Z',
              message_count: 2,
            },
          ],
    );

    renderChat();
    expect(await screen.findByText('Sara private payroll notes')).toBeInTheDocument();

    // Ensure no conversation titles or history are written to browser storage.
    expect(JSON.stringify(localStorage)).not.toContain('Sara private payroll notes');
    expect(sessionStorage.getItem('aether.session')).not.toContain('Sara private payroll notes');

    // Switch authenticated user session to User B ("omar").
    act(() => {
      session.clear();
      session.start(
        'tok-omar',
        { id: 'omar', email: 'omar@x.io', full_name: 'Omar Ali', role: 'user', created_at: '', is_deleted: false },
        3600,
      );
    });

    expect(await screen.findByText('Omar onboarding question')).toBeInTheDocument();
    expect(screen.queryByText('Sara private payroll notes')).not.toBeInTheDocument();
  });
});
