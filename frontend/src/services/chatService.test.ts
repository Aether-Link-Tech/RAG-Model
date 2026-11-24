import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ask, ChatError } from './chatService';
import { session } from './session';
import type { ChatAnswer } from '../types';

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
});
