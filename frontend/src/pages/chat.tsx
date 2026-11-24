// Ask your documents (/chat): FR-028 Q&A with streamed answers and citations.
// Backend: POST /api/v1/chat/ask (services/chatService.ts). Only your private documents
// and public documents are searched; the scope toggles map to include_private / include_public.
import { Fragment, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowUp,
  BookOpen,
  Bot,
  CircleHelp,
  FileText,
  Globe,
  Lock,
  MessageSquarePlus,
  RotateCcw,
  ScanText,
  ShieldAlert,
  Sparkles,
  Square,
  Scale,
} from 'lucide-react';
import { useSession } from '../hooks/useSession';
import { ask, ChatError } from '../services/chatService';
import type { ChatAnswer, ChatSource } from '../types';

const QUESTION_MAX = 4000; // ChatAskRequest.question max_length

const SUGGESTIONS = [
  'Summarize the key points of my documents',
  'What does the policy say about remote work?',
  'Compare what my documents say about the budget',
];

interface Turn {
  id: string;
  question: string;
  text: string; // streamed text, replaced by the final answer when done
  answer: ChatAnswer | null;
  error: string | null;
  pending: boolean;
}

/** Turns "[1]" / "[1, 2]" markers into small citation chips. */
function withCitations(text: string, sources: ChatSource[], onCite: (marker: number) => void): ReactNode[] {
  const known = new Set(sources.map((s) => s.marker));
  return text.split(/(\[\d+(?:\s*,\s*\d+)*\])/g).map((part, i) => {
    const match = /^\[(.+)\]$/.exec(part);
    if (!match) return <Fragment key={i}>{part}</Fragment>;
    const markers = match[1].split(',').map((n) => Number(n.trim()));
    if (!markers.every((m) => known.has(m))) return <Fragment key={i}>{part}</Fragment>;
    return (
      <span key={i} className="cite-group">
        {markers.map((m) => (
          <button key={m} type="button" className="cite" onClick={() => onCite(m)} aria-label={`Show source ${m}`}>
            {m}
          </button>
        ))}
      </span>
    );
  });
}

function SourceCard({ source, highlighted }: { source: ChatSource; highlighted: boolean }) {
  const [open, setOpen] = useState(false);
  const pct = Math.round(Math.max(0, Math.min(1, source.score)) * 100);
  return (
    <li className={`source-card${highlighted ? ' is-highlighted' : ''}`} id={`source-${source.marker}`}>
      <button type="button" className="source-card__head" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span className="source-card__marker">{source.marker}</span>
        <span className="source-card__name" title={source.document_name}>
          <FileText size={14} /> {source.document_name}
        </span>
        <span className="source-card__meta">
          {source.page != null && <span>p. {source.page}</span>}
          {source.source_type === 'ocr' && (
            <span className="source-card__ocr" title="Text read from an image (OCR)">
              <ScanText size={12} /> OCR
            </span>
          )}
          <span className="source-card__score" title={`Relevance ${pct}%`}>
            <i style={{ width: `${pct}%` }} />
          </span>
        </span>
      </button>
      {source.section && <p className="source-card__section">{source.section}</p>}
      <p className={`source-card__text${open ? ' is-open' : ''}`}>{source.text}</p>
    </li>
  );
}

function AnswerNotices({ answer }: { answer: ChatAnswer }) {
  return (
    <>
      {answer.injection_detected && (
        <p className="answer-note answer-note--danger">
          <ShieldAlert size={15} /> Some passages contained instructions aimed at the assistant. They were ignored.
        </p>
      )}
      {answer.insufficient && (
        <p className="answer-note answer-note--info">
          <CircleHelp size={15} /> Your documents don't contain enough information to answer this. Try rephrasing, or upload a document that covers it.
        </p>
      )}
      {!answer.insufficient && answer.uncertain && (
        <p className="answer-note answer-note--warn">
          <AlertTriangle size={15} />
          <span>
            <strong>Check this answer.</strong> {answer.uncertain_reason || 'The sources only partly support it.'}
          </span>
        </p>
      )}
      {answer.removed_claims > 0 && (
        <p className="answer-note answer-note--muted">
          {answer.removed_claims === 1
            ? '1 statement was removed because no source supported it.'
            : `${answer.removed_claims} statements were removed because no source supported them.`}
        </p>
      )}
    </>
  );
}

function ComparisonBlock({ answer }: { answer: ChatAnswer }) {
  const comparison = answer.comparison;
  if (!comparison || !comparison.pairs.length) return null;
  const label = { agree: 'Agree', differ: 'Differ', contradict: 'Contradict', unrelated: 'Unrelated' };
  return (
    <div className="comparison">
      <p className="comparison__title">
        <Scale size={15} /> Comparison: <span className={`relation relation--${comparison.relation}`}>{label[comparison.relation]}</span>
      </p>
      {comparison.pairs.map((pair, i) => (
        <div key={i} className="comparison__pair">
          <span className={`relation relation--${pair.relation}`}>{label[pair.relation]}</span>
          <blockquote>
            <strong>{pair.document_a}</strong>
            {pair.quote_a}
          </blockquote>
          <blockquote>
            <strong>{pair.document_b}</strong>
            {pair.quote_b}
          </blockquote>
        </div>
      ))}
    </div>
  );
}

export default function ChatPage() {
  const { user } = useSession();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState('');
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [includePrivate, setIncludePrivate] = useState(true);
  const [includePublic, setIncludePublic] = useState(true);
  const [highlight, setHighlight] = useState<{ turn: string; marker: number } | null>(null);
  const controller = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);

  const busy = turns.some((t) => t.pending);
  const noScope = !includePrivate && !includePublic;

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' });
  }, [turns]);

  useEffect(() => () => controller.current?.abort(), []);

  function update(id: string, patch: Partial<Turn> | ((t: Turn) => Partial<Turn>)) {
    setTurns((list) => list.map((t) => (t.id === id ? { ...t, ...(typeof patch === 'function' ? patch(t) : patch) } : t)));
  }

  async function send(question: string, reuseId?: string) {
    const text = question.trim();
    if (!text || busy || noScope) return;
    const id = reuseId ?? crypto.randomUUID();
    const fresh: Turn = { id, question: text, text: '', answer: null, error: null, pending: true };
    setTurns((list) => (reuseId ? list.map((t) => (t.id === id ? fresh : t)) : [...list, fresh]));
    if (!reuseId) setDraft('');

    const abort = new AbortController();
    controller.current = abort;
    try {
      const answer = await ask(
        { question: text, conversation_id: conversationId, include_private: includePrivate, include_public: includePublic },
        { signal: abort.signal, onToken: (piece) => update(id, (t) => ({ text: t.text + piece })) },
      );
      setConversationId(answer.conversation_id);
      // The final answer can differ from the stream (unsupported statements removed).
      update(id, { answer, text: answer.answer, pending: false });
    } catch (error) {
      if ((error as Error)?.name === 'AbortError') {
        update(id, (t) => ({ pending: false, error: t.text ? 'Stopped.' : 'Stopped before an answer arrived.' }));
      } else {
        update(id, {
          pending: false,
          error: error instanceof ChatError ? error.message : 'Something went wrong. Please try again.',
        });
      }
    } finally {
      if (controller.current === abort) controller.current = null;
      input.current?.focus();
    }
  }

  function newConversation() {
    controller.current?.abort();
    setTurns([]);
    setConversationId(null);
    setHighlight(null);
    input.current?.focus();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send(draft);
    }
  }

  function cite(turnId: string, marker: number) {
    setHighlight({ turn: turnId, marker });
    document.getElementById(`${turnId}-sources`)?.querySelector(`#source-${marker}`)?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
  }

  const firstName = user?.full_name?.trim().split(/\s+/)[0];

  return (
    <div className="chat-page">
      <section className="page-hero fade-up">
        <div>
          <span className="eyebrow">
            <Sparkles size={14} /> Assistant
          </span>
          <h1>Ask your documents</h1>
          <p className="muted">Answers come only from your private files and documents shared publicly, with sources for every claim.</p>
        </div>
        <button type="button" className="button button--ghost" onClick={newConversation} disabled={!turns.length}>
          <MessageSquarePlus size={16} /> New conversation
        </button>
      </section>

      <section className="panel chat-panel">
        <div className="chat-thread" aria-live="polite">
          {!turns.length && (
            <div className="empty-state chat-empty">
              <span className="empty-state__icon">
                <BookOpen size={28} />
              </span>
              <h3>{firstName ? `Hi ${firstName}, what would you like to know?` : 'What would you like to know?'}</h3>
              <p className="muted">
                The assistant searches your <Link to="/documents">indexed documents</Link> and cites where each answer comes from.
              </p>
              <div className="chat-suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} type="button" onClick={() => void send(s)} disabled={noScope}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((turn) => (
            <div key={turn.id} className="chat-turn">
              <div className="chat-bubble chat-bubble--user">{turn.question}</div>

              <div className="chat-answer">
                <span className="chat-answer__avatar" aria-hidden="true">
                  <Bot size={17} />
                </span>
                <div className="chat-answer__body">
                  {turn.pending && !turn.text && (
                    <p className="chat-thinking">
                      <span className="dot" />
                      <span className="dot" />
                      <span className="dot" /> Searching your documents…
                    </p>
                  )}

                  {turn.text && (
                    <div className={`chat-answer__text${turn.pending ? ' is-streaming' : ''}`}>
                      {turn.answer ? withCitations(turn.text, turn.answer.sources, (m) => cite(turn.id, m)) : turn.text}
                    </div>
                  )}

                  {turn.answer && <AnswerNotices answer={turn.answer} />}
                  {turn.answer && <ComparisonBlock answer={turn.answer} />}

                  {turn.answer && turn.answer.sources.length > 0 && (
                    <div className="chat-sources" id={`${turn.id}-sources`}>
                      <p className="chat-sources__title">Sources</p>
                      <ol>
                        {turn.answer.sources.map((s) => (
                          <SourceCard
                            key={s.marker}
                            source={s}
                            highlighted={highlight?.turn === turn.id && highlight.marker === s.marker}
                          />
                        ))}
                      </ol>
                    </div>
                  )}

                  {turn.error && (
                    <div className="inline-error chat-error" role="alert">
                      <AlertTriangle size={15} /> {turn.error}
                      <button type="button" className="link-button" onClick={() => void send(turn.question, turn.id)} disabled={busy}>
                        <RotateCcw size={13} /> Try again
                      </button>
                    </div>
                  )}
                </div>
              </div>
            </div>
          ))}
          <div ref={bottom} />
        </div>

        <form
          className="chat-composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send(draft);
          }}
        >
          <div className="chat-scope" role="group" aria-label="Search in">
            <label className={includePrivate ? 'is-on' : ''}>
              <input type="checkbox" checked={includePrivate} onChange={(e) => setIncludePrivate(e.target.checked)} />
              <Lock size={13} /> My private files
            </label>
            <label className={includePublic ? 'is-on' : ''}>
              <input type="checkbox" checked={includePublic} onChange={(e) => setIncludePublic(e.target.checked)} />
              <Globe size={13} /> Shared documents
            </label>
            {noScope && <span className="chat-scope__hint">Pick at least one place to search.</span>}
          </div>
          <div className="chat-input">
            <textarea
              ref={input}
              rows={1}
              value={draft}
              maxLength={QUESTION_MAX}
              placeholder="Ask a question about your documents…"
              aria-label="Your question"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKeyDown}
            />
            {busy ? (
              <button type="button" className="chat-send chat-send--stop" onClick={() => controller.current?.abort()} aria-label="Stop answering">
                <Square size={15} />
              </button>
            ) : (
              <button type="submit" className="chat-send" disabled={!draft.trim() || noScope} aria-label="Send question">
                <ArrowUp size={18} />
              </button>
            )}
          </div>
          <p className="chat-hint muted small">Enter to send · Shift + Enter for a new line</p>
        </form>
      </section>
    </div>
  );
}
