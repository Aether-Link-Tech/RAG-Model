import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import api from './api';
import { ask, ChatError, deleteConversation, getConversation, listConversations } from './chatService';
import { session } from './session';
import type { ChatAnswer, ConversationDetail, ConversationSummary } from '../types';

const final: ChatAnswer = {
  conversation_id: 'c1', message_id: 'm1', answer: 'Final answer [1].',
  sources: [{ marker: 1, document_id: 'd', document_name: 'a.pdf', page: 1, section: null, source_type: 'text', text: 't', score: 0.9 }],
  insufficient: false, uncertain: false, uncertain_reason: null, claims: [], removed_claims: 0,
  injection_detected: false, comparison: null,
};

/** A streaming Response whose body arrives in the given (arbitrarily split) chunks. */
function sse(chunks: string[], status = 200) {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((c) => controller.enqueue(new TextEncoder().encode(c)));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { 'Content-Type': 'text/event-stream' } });
}

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  session.start('tok', { id: 'u', email: 'u@x.io', role: 'user', created_at: '', is_deleted: false }, 3600);
});
afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

describe('chatService.ask', () => {
  it('streams tokens and returns the final answer, even when events are split across chunks', async () => {
    const done = `event: done\ndata: ${JSON.stringify(final)}\n\n`;
    fetchMock.mockResolvedValue(
      sse(['event: token\ndata: {"text":"Hel', 'lo"}\n\nevent: token\r\ndata: {"text":" there"}\r\n\r\n', done.slice(0, 20), done.slice(20)]),
    );
    const tokens: string[] = [];
    const answer = await ask({ question: 'hi' }, { onToken: (t) => tokens.push(t) });

    expect(tokens).toEqual(['Hello', ' there']);
    expect(answer).toEqual(final);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v1/chat/ask');
    expect(init.headers.Authorization).toBe('Bearer tok');
    expect(init.headers.Accept).toBe('text/event-stream');
    expect(JSON.parse(init.body)).toEqual({ question: 'hi' });
  });

  it('turns an SSE error event into a ChatError with the server message', async () => {
    fetchMock.mockResolvedValue(sse(['event: error\ndata: {"detail":"The language model is unavailable."}\n\n']));
    await expect(ask({ question: 'hi' })).rejects.toThrow('The language model is unavailable.');
  });

  it('reports HTTP errors with the FastAPI detail', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: 'Search is unavailable right now.' }), { status: 503 }));
    const error = await ask({ question: 'hi' }).catch((e) => e);
    expect(error).toBeInstanceOf(ChatError);
    expect(error.status).toBe(503);
    expect(error.message).toBe('Search is unavailable right now.');
  });

  it('accepts a plain JSON answer', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(final), { headers: { 'Content-Type': 'application/json' } }));
    expect(await ask({ question: 'hi' })).toEqual(final);
  });

  it('says so when the backend cannot be reached', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(ask({ question: 'hi' })).rejects.toThrow(/cannot reach the server/i);
  });

  it('fails clearly when the stream ends without a final answer', async () => {
    fetchMock.mockResolvedValue(sse(['event: token\ndata: {"text":"partial"}\n\n']));
    await expect(ask({ question: 'hi' })).rejects.toThrow(/cut off/i);
  });

  it('ends the session on 401 when there is no refresh token', async () => {
    const expired = vi.fn();
    window.addEventListener('auth:expired', expired);
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: 'Not authenticated' }), { status: 401 }));
    await expect(ask({ question: 'hi' })).rejects.toBeInstanceOf(ChatError);
    expect(session.isAuthenticated()).toBe(false);
    expect(expired).toHaveBeenCalled();
    window.removeEventListener('auth:expired', expired);
  });

  it('stops emitting tokens or resolving when aborted mid-stream', async () => {
    const controller = new AbortController();
    const tokens: string[] = [];
    const done = `event: done\ndata: ${JSON.stringify(final)}\n\n`;
    const body = new ReadableStream<Uint8Array>({
      pull(streamController) {
        streamController.enqueue(new TextEncoder().encode('event: token\ndata: {"text":"First"}\n\n'));
        controller.abort();
        streamController.enqueue(new TextEncoder().encode(done));
        streamController.close();
      },
    });
    fetchMock.mockResolvedValue(new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));

    await expect(ask({ question: 'hi' }, { signal: controller.signal, onToken: (t) => tokens.push(t) })).rejects.toThrow(
      /aborted/i,
    );
    expect(tokens).toEqual([]);
  });
});

describe('chatService conversation history APIs', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('lists, retrieves, and deletes conversations through the authenticated API client', async () => {
    const summaries: ConversationSummary[] = [
      {
        id: 'conv-1',
        title: 'Leave policy question',
        created_at: '2026-11-26T08:00:00Z',
        updated_at: '2026-11-26T08:01:00Z',
        message_count: 2,
      },
    ];
    const detail: ConversationDetail = {
      ...summaries[0],
      messages: [
        {
          id: 'u1',
          conversation_id: 'conv-1',
          role: 'user',
          content: 'Leave policy question',
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
          id: 'a1',
          conversation_id: 'conv-1',
          role: 'assistant',
          content: 'Final answer [1].',
          sources: final.sources,
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

    const getSpy = vi.spyOn(api, 'get');
    const delSpy = vi.spyOn(api, 'delete');
    getSpy.mockResolvedValueOnce({ data: summaries });
    getSpy.mockResolvedValueOnce({ data: detail });
    delSpy.mockResolvedValueOnce({ status: 204 });

    expect(await listConversations()).toEqual(summaries);
    expect(getSpy).toHaveBeenNthCalledWith(1, '/chat/conversations');

    expect(await getConversation('conv-1')).toEqual(detail);
    expect(getSpy).toHaveBeenNthCalledWith(2, '/chat/conversations/conv-1');

    await deleteConversation('conv-1');
    expect(delSpy).toHaveBeenCalledWith('/chat/conversations/conv-1');
  });
});
