// Q&A over your documents (FR-028): POST /api/v1/chat/ask
// The answer is streamed with Server-Sent Events (Accept: text/event-stream):
//   "token" {text} ... then "done" {ChatAnswer}  or  "error" {detail}
// axios cannot read a streaming body in the browser, so this uses fetch with the same
// Bearer token / refresh rules as services/api.ts.
import { API_URL, USE_MOCK } from './config';
import api, { getErrorMessage, refreshSession, statusOf } from './api';
import { session } from './session';
import { mockDocuments, wait } from './mockData';
import type {
  ChatAnswer,
  ChatAskRequest,
  ConversationDetail,
  ConversationSummary,
} from '../types';

export class ChatError extends Error {
  constructor(
    message: string,
    public status?: number,
  ) {
    super(message);
    this.name = 'ChatError';
  }
}

export interface AskHandlers {
  /** Called for every streamed piece of the answer. */
  onToken?: (text: string) => void;
  signal?: AbortSignal;
}

const OFFLINE = 'Cannot reach the server. Make sure the backend is running.';

function messageFor(status: number, detail: unknown): string {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg).replace(/^Value error, /, '');
  if (status === 401) return 'Your session has expired. Please sign in again.';
  if (status === 503) return 'The assistant is unavailable right now. Please try again in a moment.';
  return 'Something went wrong. Please try again.';
}

async function post(body: ChatAskRequest, signal?: AbortSignal, retried = false): Promise<Response> {
  let token = session.token;
  if (!token && session.refreshToken) token = await refreshSession();

  let response: Response;
  try {
    response = await fetch(`${API_URL}/chat/ask`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') throw error;
    throw new ChatError(OFFLINE);
  }

  // Same rule as the axios interceptor: one silent refresh, then the session ends.
  if (response.status === 401) {
    if (!retried && session.refreshToken && (await refreshSession())) return post(body, signal, true);
    if (session.isAuthenticated()) {
      session.clear();
      window.dispatchEvent(new CustomEvent('auth:expired'));
    }
  }
  return response;
}

/** Parses one "event: x\ndata: {...}" block. */
function parseEvent(block: string): { event: string; data: unknown } | null {
  let event = 'message';
  const data: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
  }
  if (!data.length) return null;
  try {
    return { event, data: JSON.parse(data.join('\n')) };
  } catch {
    return null;
  }
}

export async function ask(request: ChatAskRequest, { onToken, signal }: AskHandlers = {}): Promise<ChatAnswer> {
  if (USE_MOCK) return mockAsk(request, onToken, signal);

  const response = await post(request, signal);

  if (!response.ok) {
    let detail: unknown;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail;
    } catch {
      /* not JSON */
    }
    throw new ChatError(messageFor(response.status, detail), response.status);
  }

  // A proxy may hand back plain JSON instead of a stream; accept both.
  if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) {
    const answer = (await response.json()) as ChatAnswer;
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    onToken?.(answer.answer);
    return answer;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let streamed = '';

  for (;;) {
    const { value, done } = await reader.read();
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    buffer += decoder.decode(value, { stream: !done });

    let boundary = buffer.search(/\r?\n\r?\n/);
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary).replace(/^\r?\n\r?\n/, '');
      const parsed = parseEvent(block);
      if (parsed?.event === 'token') {
        const text = (parsed.data as { text?: string }).text ?? '';
        streamed += text;
        onToken?.(text);
      } else if (parsed?.event === 'done') {
        return parsed.data as ChatAnswer;
      } else if (parsed?.event === 'error') {
        throw new ChatError(messageFor(503, (parsed.data as { detail?: unknown }).detail), 503);
      }
      boundary = buffer.search(/\r?\n\r?\n/);
    }

    if (done) break;
  }

  throw new ChatError(
    streamed ? 'The answer was cut off. Please try again.' : 'The server closed the connection before answering.',
  );
}

export async function listConversations(): Promise<ConversationSummary[]> {
  if (USE_MOCK) return [];
  try {
    const { data } = await api.get<ConversationSummary[]>('/chat/conversations');
    return data;
  } catch (error) {
    throw new ChatError(getErrorMessage(error, 'Unable to load conversations.'), statusOf(error));
  }
}

export async function getConversation(conversationId: string): Promise<ConversationDetail> {
  if (USE_MOCK) {
    throw new ChatError('Conversation not found', 404);
  }
  try {
    const { data } = await api.get<ConversationDetail>(
      `/chat/conversations/${encodeURIComponent(conversationId)}`,
    );
    return data;
  } catch (error) {
    throw new ChatError(getErrorMessage(error, 'Unable to load conversation history.'), statusOf(error));
  }
}

export async function deleteConversation(conversationId: string): Promise<void> {
  if (USE_MOCK) return;
  try {
    await api.delete(`/chat/conversations/${encodeURIComponent(conversationId)}`);
  } catch (error) {
    throw new ChatError(getErrorMessage(error, 'Unable to delete conversation.'), statusOf(error));
  }
}

// ---------- demo mode (VITE_USE_MOCK=true) ----------
async function mockAsk(request: ChatAskRequest, onToken?: (t: string) => void, signal?: AbortSignal): Promise<ChatAnswer> {
  const doc = mockDocuments.find((d) => d.status === 'indexed') ?? mockDocuments[0];
  const answer = `This is a demo answer to "${request.question.trim()}". In demo mode no model is called, but the layout, streaming and citations work like the real thing [1].`;
  for (const word of answer.split(/(?<= )/)) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    await wait(25);
    onToken?.(word);
  }
  return {
    conversation_id: request.conversation_id ?? 'demo-conversation',
    message_id: crypto.randomUUID(),
    answer,
    sources: [
      {
        marker: 1,
        document_id: doc.id,
        document_name: doc.filename,
        page: 2,
        section: 'Summary',
        source_type: 'text',
        text: 'Demo passage from the document that supports the answer.',
        score: 0.82,
      },
    ],
    insufficient: false,
    uncertain: false,
    uncertain_reason: null,
    claims: [],
    removed_claims: 0,
    injection_detected: false,
    comparison: null,
  };
}
