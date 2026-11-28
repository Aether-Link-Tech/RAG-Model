// Ask your documents (/chat): FR-028 Q&A with streamed answers, citations, and three-zone workspace.
// Backend: POST /api/v1/chat/ask (services/chatService.ts). Only your private documents
// and public documents are searched; the scope toggles map to include_private / include_public.
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  BookOpen,
  Bot,
  Check,
  CircleHelp,
  Copy,
  FileUp,
  Globe,
  Lock,
  MessageSquarePlus,
  PanelLeft,
  RotateCcw,
  Scale,
  ShieldAlert,
  Square,
} from 'lucide-react';
import ChatSidebar, { type SessionThread } from '../components/ChatSidebar';
import LatticeLoader, { type LatticeStatus } from '../components/LatticeLoader';
import SourcesPanel, { SourceCard, formatSourcesText } from '../components/SourcesPanel';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { useSession } from '../hooks/useSession';
import { ask, ChatError, getConversation, listConversations } from '../services/chatService';
import { listCollections } from '../services/collectionService';
import { listDocuments } from '../services/documentService';
import type { ChatAnswer, ChatSource, Collection, ConversationMessage, Document } from '../types';

const QUESTION_MAX = 4000; // ChatAskRequest.question max_length

// Seed questions matching evaluation/dataset.py titles so real fixture docs surface authentic prompts.
const SEED_QUESTIONS_BY_DOC: Record<string, string> = {
  'password policy': 'How long must a password be?',
  'remote work policy': 'How many days a week can I work from home?',
  'annual leave policy': 'How many paid leave days do full-time staff get?',
  'data incident response': 'How quickly must a data incident be reported?',
  'travel and expenses policy': 'What is the daily meal allowance on business trips?',
  'laptop and equipment policy': 'How often are staff laptops replaced?',
  'email and messaging policy': 'What is the maximum email attachment size?',
  'network and wifi access': 'Which Wi-Fi network do staff use?',
  'software installation policy': 'Where can staff install software from?',
  'backup and retention policy': 'How long are server backups kept?',
  'visitor and building access': 'When do staff access cards open the building?',
  'training and onboarding': 'Within how long must new staff finish the security awareness course?',
};

interface Turn {
  id: string;
  question: string;
  text: string;
  answer: ChatAnswer | null;
  error: string | null;
  pending: boolean;
  startedAt: number;
  elapsedSeconds?: number;
}

interface Thread {
  localId: string;
  conversationId: string | null;
  title: string;
  messageCount: number;
  turns: Turn[];
  loaded: boolean;
  loading: boolean;
  loadError: string | null;
}

function messagesToTurns(conversationId: string, messages: ConversationMessage[]): Turn[] {
  const turns: Turn[] = [];
  for (let i = 0; i < messages.length; i += 1) {
    const current = messages[i];
    if (current.role === 'user') {
      const next = messages[i + 1];
      if (next && next.role === 'assistant') {
        const answer: ChatAnswer = {
          conversation_id: conversationId,
          message_id: next.id,
          answer: next.content,
          sources: next.sources ?? [],
          insufficient: Boolean(next.insufficient),
          uncertain: Boolean(next.uncertain),
          uncertain_reason: next.uncertain_reason ?? null,
          claims: next.claims ?? [],
          removed_claims: next.removed_claims ?? 0,
          injection_detected: Boolean(next.injection_detected),
          comparison: next.comparison ?? null,
        };
        turns.push({
          id: next.id,
          question: current.content,
          text: next.content,
          answer,
          error: null,
          pending: false,
          startedAt: Date.parse(current.created_at) || Date.now(),
        });
        i += 1;
      } else {
        turns.push({
          id: current.id,
          question: current.content,
          text: '',
          answer: null,
          error: null,
          pending: false,
          startedAt: Date.parse(current.created_at) || Date.now(),
        });
      }
    } else if (current.role === 'assistant') {
      const answer: ChatAnswer = {
        conversation_id: conversationId,
        message_id: current.id,
        answer: current.content,
        sources: current.sources ?? [],
        insufficient: Boolean(current.insufficient),
        uncertain: Boolean(current.uncertain),
        uncertain_reason: current.uncertain_reason ?? null,
        claims: current.claims ?? [],
        removed_claims: current.removed_claims ?? 0,
        injection_detected: Boolean(current.injection_detected),
        comparison: current.comparison ?? null,
      };
      turns.push({
        id: current.id,
        question: '',
        text: current.content,
        answer,
        error: null,
        pending: false,
        startedAt: Date.parse(current.created_at) || Date.now(),
      });
    }
  }
  return turns;
}

function normalizeDocTitle(filename: string): string {
  return filename.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim().toLowerCase();
}

function buildSuggestedQuestions(docs: Document[]): string[] {
  if (!docs.length) return [];
  const suggestions: string[] = [];
  const pushUnique = (q: string) => {
    if (!suggestions.includes(q) && suggestions.length < 3) suggestions.push(q);
  };

  for (const doc of docs) {
    const seed = SEED_QUESTIONS_BY_DOC[normalizeDocTitle(doc.filename)];
    if (seed) pushUnique(seed);
  }

  if (docs.length >= 2 && suggestions.length < 3) {
    pushUnique(`Compare what "${docs[0].filename}" and "${docs[1].filename}" cover`);
  }
  if (suggestions.length < 3) {
    pushUnique(`Summarize the key points in "${docs[0].filename}"`);
  }
  if (docs.length >= 2 && suggestions.length < 3) {
    pushUnique(`What requirements or deadlines are listed in "${docs[1].filename}"?`);
  }
  if (suggestions.length < 3) {
    pushUnique(`What specific rules, figures, or obligations appear in "${docs[0].filename}"?`);
  }
  if (suggestions.length < 3) {
    pushUnique(`Which topics are not covered in "${docs[0].filename}"?`);
  }
  return suggestions.slice(0, 3);
}

/** Turns "[1]" / "[1, 2]" markers into interactive citation buttons. */
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
          <button
            key={m}
            type="button"
            className="cite tabular-nums"
            onClick={() => onCite(m)}
            aria-label={`Show source ${m}`}
          >
            {m}
          </button>
        ))}
      </span>
    );
  });
}

function AnswerNotices({ answer }: { answer: ChatAnswer }) {
  return (
    <>
      {answer.injection_detected && (
        <p className="answer-note answer-note--danger">
          <ShieldAlert size={15} aria-hidden="true" />
          <span>Some passages contained instructions aimed at the assistant. They were ignored.</span>
        </p>
      )}
      {answer.insufficient && (
        <p className="answer-note answer-note--info">
          <CircleHelp size={15} aria-hidden="true" />
          <span>
            Your documents don't contain enough information to answer this. Try rephrasing, or upload a document that covers it.
          </span>
        </p>
      )}
      {!answer.insufficient && answer.uncertain && (
        <p className="answer-note answer-note--warn">
          <AlertTriangle size={15} aria-hidden="true" />
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
        <Scale size={15} aria-hidden="true" />
        <span>Comparison:</span>
        <span className={`relation relation--${comparison.relation}`}>{label[comparison.relation]}</span>
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

function TurnActions({ answer }: { answer: ChatAnswer }) {
  const [copiedAnswer, setCopiedAnswer] = useState(false);
  const [copiedSources, setCopiedSources] = useState(false);

  async function copyAnswer() {
    try {
      await navigator.clipboard?.writeText(answer.answer);
      setCopiedAnswer(true);
      setTimeout(() => setCopiedAnswer(false), 1800);
    } catch {
      /* clipboard blocked */
    }
  }

  async function copySources() {
    if (!answer.sources.length) return;
    try {
      await navigator.clipboard?.writeText(formatSourcesText(answer.sources));
      setCopiedSources(true);
      setTimeout(() => setCopiedSources(false), 1800);
    } catch {
      /* clipboard blocked */
    }
  }

  return (
    <div className="turn-actions">
      <button type="button" className="turn-action" onClick={copyAnswer}>
        {copiedAnswer ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
        <span>{copiedAnswer ? 'Copied' : 'Copy answer'}</span>
      </button>
      {answer.sources.length > 0 && (
        <button type="button" className="turn-action" onClick={copySources}>
          {copiedSources ? <Check size={13} aria-hidden="true" /> : <BookOpen size={13} aria-hidden="true" />}
          <span>{copiedSources ? 'Sources copied' : 'Copy sources'}</span>
        </button>
      )}
    </div>
  );
}

export default function ChatPage() {
  const { user } = useSession();
  const currentUserId = user?.id ?? null;
  const isCompact = useMediaQuery('(max-width: 1023px)');
  const [threads, setThreads] = useState<Thread[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(true);
  const [conversationsError, setConversationsError] = useState<string | null>(null);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [includePrivate, setIncludePrivate] = useState(true);
  const [includePublic, setIncludePublic] = useState(true);
  const [docs, setDocs] = useState<Document[] | null>(null);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [selectedSourceTurnId, setSelectedSourceTurnId] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<{ turn: string; marker: number } | null>(null);
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);

  const controller = useRef<AbortController | null>(null);
  const activeThreadIdRef = useRef<string | null>(null);
  const loadingConversationIds = useRef<Set<string>>(new Set());
  const sessionEpochRef = useRef(0);
  const threadRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const fetchConversations = useCallback(async (epoch: number) => {
    setConversationsLoading(true);
    setConversationsError(null);
    try {
      const summaries = await listConversations();
      if (sessionEpochRef.current !== epoch) return;
      setThreads((current) => {
        const byConvId = new Map(
          current.filter((t) => t.conversationId).map((t) => [t.conversationId!, t]),
        );
        const localUnsaved = current.filter((t) => !t.conversationId);
        const merged: Thread[] = summaries.map((s) => {
          const existing = byConvId.get(s.id);
          if (existing) {
            return {
              ...existing,
              title: s.title,
              messageCount: Math.max(existing.messageCount, s.message_count),
            };
          }
          return {
            localId: s.id,
            conversationId: s.id,
            title: s.title,
            messageCount: s.message_count,
            turns: [],
            loaded: false,
            loading: false,
            loadError: null,
          };
        });
        return [...localUnsaved, ...merged];
      });
    } catch (error) {
      if (sessionEpochRef.current !== epoch) return;
      const message =
        error instanceof ChatError && error.message
          ? error.message
          : 'Unable to load conversations.';
      setConversationsError(message);
    } finally {
      if (sessionEpochRef.current === epoch) {
        setConversationsLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    sessionEpochRef.current += 1;
    const epoch = sessionEpochRef.current;

    controller.current?.abort();
    controller.current = null;
    loadingConversationIds.current.clear();

    setThreads([]);
    activeThreadIdRef.current = null;
    setActiveThreadId(null);
    setSelectedSourceTurnId(null);
    setHighlight(null);
    setShowJumpToBottom(false);
    setDraft('');
    setDocs(null);
    setCollections([]);

    if (!currentUserId) {
      setConversationsLoading(false);
      setConversationsError(null);
      return undefined;
    }

    let active = true;
    listDocuments()
      .then((list) => active && sessionEpochRef.current === epoch && setDocs(list))
      .catch(() => active && sessionEpochRef.current === epoch && setDocs([]));
    listCollections()
      .then((list) => active && sessionEpochRef.current === epoch && setCollections(list))
      .catch(() => {
        /* collections remain optional context */
      });
    void fetchConversations(epoch);

    return () => {
      active = false;
      sessionEpochRef.current += 1;
    };
  }, [currentUserId, fetchConversations]);

  useEffect(() => () => controller.current?.abort(), []);

  const activeThread = useMemo(
    () => threads.find((t) => t.localId === activeThreadId) ?? null,
    [threads, activeThreadId],
  );
  const turns = useMemo(() => activeThread?.turns ?? [], [activeThread]);
  const busy = turns.some((t) => t.pending);
  const historyBusy = Boolean(activeThread?.loading);
  const noScope = !includePrivate && !includePublic;

  const indexedInScope = useMemo(() => {
    if (!docs) return [];
    return docs.filter(
      (d) =>
        d.status === 'indexed' &&
        ((d.visibility === 'private' && includePrivate) || (d.visibility === 'public' && includePublic)),
    );
  }, [docs, includePrivate, includePublic]);

  const suggestions = useMemo(() => buildSuggestedQuestions(indexedInScope), [indexedInScope]);

  const scrollToBottom = useCallback(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, []);

  // Follow stream ONLY when the user is already near the bottom.
  useEffect(() => {
    if (isAtBottomRef.current) {
      scrollToBottom();
    }
  }, [turns, scrollToBottom]);

  function handleThreadScroll() {
    const el = threadRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 64;
    isAtBottomRef.current = atBottom;
    setShowJumpToBottom(!atBottom && turns.length > 0);
  }

  function updateTurn(threadId: string, turnId: string, patch: Partial<Turn> | ((t: Turn) => Partial<Turn>)) {
    setThreads((list) =>
      list.map((th) => {
        if (th.localId !== threadId) return th;
        return {
          ...th,
          turns: th.turns.map((t) => (t.id === turnId ? { ...t, ...(typeof patch === 'function' ? patch(t) : patch) } : t)),
        };
      }),
    );
  }

  async function loadThreadHistory(localId: string, conversationId: string) {
    if (loadingConversationIds.current.has(conversationId)) return;
    loadingConversationIds.current.add(conversationId);
    const epoch = sessionEpochRef.current;

    setThreads((list) =>
      list.map((th) =>
        th.localId === localId ? { ...th, loading: true, loadError: null } : th,
      ),
    );

    try {
      const detail = await getConversation(conversationId);
      if (sessionEpochRef.current !== epoch) return;
      const restoredTurns = messagesToTurns(detail.id, detail.messages);
      setThreads((list) =>
        list.map((th) =>
          th.localId === localId || th.conversationId === detail.id
            ? {
                ...th,
                conversationId: detail.id,
                title: detail.title,
                messageCount: detail.message_count,
                turns: restoredTurns,
                loaded: true,
                loading: false,
                loadError: null,
              }
            : th,
        ),
      );
      if (activeThreadIdRef.current === localId) {
        const lastWithSources = [...restoredTurns].reverse().find((t) => t.answer?.sources.length);
        const lastTurn = restoredTurns[restoredTurns.length - 1];
        setSelectedSourceTurnId(lastWithSources?.id ?? lastTurn?.id ?? null);
      }
    } catch (error) {
      if (sessionEpochRef.current !== epoch) return;
      const message =
        error instanceof ChatError && error.message
          ? error.message
          : 'Unable to load conversation history.';
      setThreads((list) =>
        list.map((th) =>
          th.localId === localId ? { ...th, loading: false, loadError: message } : th,
        ),
      );
    } finally {
      loadingConversationIds.current.delete(conversationId);
    }
  }

  async function send(question: string, reuseId?: string) {
    const text = question.trim();
    if (!text || busy || historyBusy || noScope || controller.current) return;

    const epoch = sessionEpochRef.current;
    const targetThreadId = activeThreadId ?? crypto.randomUUID();
    const convId = activeThread?.conversationId ?? null;
    const turnId = reuseId ?? crypto.randomUUID();
    const startedAt = Date.now();
    const fresh: Turn = {
      id: turnId,
      question: text,
      text: '',
      answer: null,
      error: null,
      pending: true,
      startedAt,
    };

    if (!activeThreadId) {
      setThreads((list) => [
        {
          localId: targetThreadId,
          conversationId: null,
          title: text,
          messageCount: 0,
          turns: [fresh],
          loaded: true,
          loading: false,
          loadError: null,
        },
        ...list,
      ]);
      activeThreadIdRef.current = targetThreadId;
      setActiveThreadId(targetThreadId);
    } else {
      setThreads((list) =>
        list.map((th) =>
          th.localId !== targetThreadId
            ? th
            : {
                ...th,
                turns: reuseId ? th.turns.map((t) => (t.id === turnId ? fresh : t)) : [...th.turns, fresh],
              },
        ),
      );
    }

    if (!reuseId) setDraft('');
    isAtBottomRef.current = true;
    setShowJumpToBottom(false);
    setSelectedSourceTurnId(turnId);
    setHighlight(null);

    const abort = new AbortController();
    controller.current = abort;

    try {
      const answer = await ask(
        {
          question: text,
          conversation_id: convId,
          include_private: includePrivate,
          include_public: includePublic,
        },
        {
          signal: abort.signal,
          onToken: (piece) => {
            if (abort.signal.aborted || sessionEpochRef.current !== epoch) return;
            updateTurn(targetThreadId, turnId, (t) => (t.pending ? { text: t.text + piece } : {}));
          },
        },
      );

      if (abort.signal.aborted || sessionEpochRef.current !== epoch) return;

      setThreads((list) => {
        let updatedThread: Thread | null = null;
        const rest: Thread[] = [];
        for (const th of list) {
          if (th.localId !== targetThreadId) {
            rest.push(th);
            continue;
          }
          const nextTurns = th.turns.map((t) =>
            t.id === turnId && t.pending
              ? {
                  ...t,
                  answer,
                  text: answer.answer,
                  pending: false,
                  error: null,
                  elapsedSeconds: (Date.now() - t.startedAt) / 1000,
                }
              : t,
          );
          updatedThread = {
            ...th,
            conversationId: answer.conversation_id,
            messageCount: Math.max(th.messageCount + 2, nextTurns.length * 2),
            turns: nextTurns,
            loaded: true,
            loading: false,
            loadError: null,
          };
        }
        return updatedThread ? [updatedThread, ...rest] : list;
      });
      setSelectedSourceTurnId((prev) => (prev === turnId ? turnId : prev));
    } catch (error) {
      if (sessionEpochRef.current !== epoch) return;
      if ((error as Error)?.name === 'AbortError') {
        updateTurn(targetThreadId, turnId, (t) => ({
          pending: false,
          error: t.text ? 'Stopped.' : 'Stopped before an answer arrived.',
          elapsedSeconds: (Date.now() - t.startedAt) / 1000,
        }));
      } else {
        const message =
          error instanceof ChatError && error.message
            ? error.message
            : 'Unable to get an answer right now. Please try again.';
        updateTurn(targetThreadId, turnId, (t) => ({
          pending: false,
          error: message,
          elapsedSeconds: (Date.now() - t.startedAt) / 1000,
        }));
      }
    } finally {
      if (controller.current === abort) controller.current = null;
      inputRef.current?.focus();
    }
  }

  function newConversation() {
    controller.current?.abort();
    controller.current = null;
    activeThreadIdRef.current = null;
    setActiveThreadId(null);
    setSelectedSourceTurnId(null);
    setHighlight(null);
    setShowJumpToBottom(false);
    inputRef.current?.focus();
  }

  function selectThread(id: string) {
    controller.current?.abort();
    controller.current = null;
    activeThreadIdRef.current = id;
    setActiveThreadId(id);
    setHighlight(null);
    const chosen = threads.find((t) => t.localId === id);
    if (!chosen) {
      setSelectedSourceTurnId(null);
      return;
    }
    if (chosen.loaded) {
      const lastWithSources = [...chosen.turns].reverse().find((t) => t.answer?.sources.length);
      const lastTurn = chosen.turns[chosen.turns.length - 1];
      setSelectedSourceTurnId(lastWithSources?.id ?? lastTurn?.id ?? null);
    } else if (chosen.conversationId) {
      setSelectedSourceTurnId(null);
      void loadThreadHistory(chosen.localId, chosen.conversationId);
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send(draft);
    }
  }

  function cite(turnId: string, marker: number) {
    setSelectedSourceTurnId(turnId);
    setHighlight({ turn: turnId, marker });
    requestAnimationFrame(() => {
      document
        .querySelector(`[data-turn-source="${turnId}-${marker}"]`)
        ?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
    });
  }

  const sidebarThreads: SessionThread[] = threads.map((t) => ({
    id: t.localId,
    title: t.title,
    turnCount: t.loaded ? t.turns.length : Math.ceil(t.messageCount / 2),
    active: t.localId === activeThreadId,
  }));

  // Resolve which turn's sources to show in the desktop right panel.
  const activeSourceTurn = useMemo(() => {
    if (!turns.length) return null;
    const explicit = turns.find((t) => t.id === selectedSourceTurnId);
    if (explicit) return explicit;
    return [...turns].reverse().find((t) => (t.answer?.sources.length ?? 0) > 0) ?? turns[turns.length - 1];
  }, [turns, selectedSourceTurnId]);

  const turnOptions = useMemo(
    () =>
      turns
        .map((t, i) => ({
          id: t.id,
          index: i + 1,
          question: t.question,
          sourceCount: t.answer?.sources.length ?? 0,
        }))
        .filter((o) => o.sourceCount > 0),
    [turns],
  );

  const hasDocs = docs !== null && docs.length > 0;
  const hasReadyDocs = indexedInScope.length > 0;

  return (
    <div className="chat-workspace">
      <ChatSidebar
        openMobile={sidebarOpen}
        onCloseMobile={() => setSidebarOpen(false)}
        threads={sidebarThreads}
        conversationsLoading={conversationsLoading}
        conversationsError={conversationsError}
        onRetryConversations={() => void fetchConversations(sessionEpochRef.current)}
        onSelectThread={selectThread}
        onNewConversation={newConversation}
        canStartNew={Boolean(activeThreadId || turns.length)}
        docs={docs}
        collections={collections}
      />

      <section className="chat-center" aria-label="Conversation">
        <header className="chat-center__head">
          <div className="chat-center__title-wrap">
            <button
              type="button"
              className="icon-button chat-center__menu"
              aria-label="Open conversations and documents sidebar"
              onClick={() => setSidebarOpen(true)}
            >
              <PanelLeft size={18} />
            </button>
            <h1 className="chat-center__title">Ask your documents</h1>
          </div>
          <div className="chat-center__actions">
            {turns.length > 0 && (
              <button type="button" className="button button--ghost button--sm" onClick={newConversation}>
                <MessageSquarePlus size={14} aria-hidden="true" />
                <span>New conversation</span>
              </button>
            )}
          </div>
        </header>

        <div className="chat-thread" data-testid="chat-thread" ref={threadRef} onScroll={handleThreadScroll} aria-live="polite">
          <div className="chat-reading-col">
            {!turns.length && (
              <div className="chat-empty">
                {activeThread?.loading ? (
                  <div className="chat-empty__skeleton" role="status" aria-busy="true" aria-label="Loading conversation">
                    <span className="skeleton skeleton--line" />
                    <span className="skeleton skeleton--line" />
                    <span className="skeleton skeleton--line skeleton--short" />
                  </div>
                ) : activeThread?.loadError ? (
                  <div className="inline-error chat-error" role="alert">
                    <AlertTriangle size={15} aria-hidden="true" />
                    <span>{activeThread.loadError}</span>
                    {activeThread.conversationId && (
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => void loadThreadHistory(activeThread.localId, activeThread.conversationId!)}
                      >
                        <RotateCcw size={13} aria-hidden="true" /> Try again
                      </button>
                    )}
                  </div>
                ) : docs === null ? (
                  <div className="chat-empty__skeleton" aria-busy="true" aria-label="Loading workspace">
                    <span className="skeleton skeleton--line" />
                    <span className="skeleton skeleton--line skeleton--short" />
                  </div>
                ) : !hasDocs ? (
                  <>
                    <span className="chat-empty__icon" aria-hidden="true">
                      <FileUp size={22} />
                    </span>
                    <h2>No documents loaded</h2>
                    <p className="muted">
                      Upload a PDF, DOCX, or TXT file to index your knowledge base and ask grounded questions with source citations.
                    </p>
                    <Link to="/documents" className="button button--primary">
                      <FileUp size={16} aria-hidden="true" /> Upload documents
                    </Link>
                  </>
                ) : !hasReadyDocs ? (
                  <>
                    <span className="chat-empty__icon" aria-hidden="true">
                      <BookOpen size={22} />
                    </span>
                    <h2>
                      {docs.some((d) => d.status === 'pending' || d.status === 'processing')
                        ? 'Documents are still processing'
                        : 'No indexed documents in this scope'}
                    </h2>
                    <p className="muted">
                      {noScope
                        ? 'Enable private or shared documents below to search your workspace.'
                        : 'Check your document status or adjust the search scope below.'}
                    </p>
                    <Link to="/documents" className="button button--ghost">
                      View documents
                    </Link>
                  </>
                ) : (
                  <>
                    <span className="chat-empty__icon" aria-hidden="true">
                      <BookOpen size={22} />
                    </span>
                    <h2>Ask about your documents</h2>
                    <p className="muted">
                      Searching <strong className="tabular-nums">{indexedInScope.length}</strong> indexed document
                      {indexedInScope.length === 1 ? '' : 's'}. Answers cite exact source passages.
                    </p>
                    <div className="chat-suggestions" aria-label="Suggested questions">
                      {suggestions.map((s) => (
                        <button key={s} type="button" onClick={() => void send(s)} disabled={noScope}>
                          {s}
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}

            {turns.map((turn) => {
              const loaderStatus: LatticeStatus = turn.pending ? 'working' : turn.error ? 'error' : 'done';
              return (
              <article key={turn.id} className="chat-turn">
                <div className="chat-bubble--user">{turn.question}</div>

                <div className="chat-answer">
                  <span className="chat-answer__avatar" aria-hidden="true">
                    <Bot size={16} />
                  </span>
                  <div className="chat-answer__body">
                    <div className="chat-answer__status">
                      <LatticeLoader
                        status={loaderStatus}
                        label="Thinking"
                        doneLabel={turn.elapsedSeconds != null ? 'Done in' : 'Answered'}
                        errorLabel="Failed after"
                        pattern="orbit"
                        grid={3}
                        shape="round"
                        doneColor="#22c55e"
                        errorColor="#ef4444"
                        cellSize={6}
                        gap={2}
                        fontSize={14}
                        step={90}
                        idleOpacity={0.15}
                        glow={false}
                        glowColor=""
                        showTimer={turn.pending || turn.elapsedSeconds != null}
                        elapsed={turn.pending ? undefined : turn.elapsedSeconds}
                      />
                    </div>

                    {turn.pending && !turn.text && (
                      <div className="chat-thinking__skeleton" aria-hidden="true">
                        <span className="skeleton skeleton--line" />
                        <span className="skeleton skeleton--line" />
                        <span className="skeleton skeleton--line skeleton--short" />
                      </div>
                    )}

                    {turn.text && (
                      <div className={`chat-answer__text${turn.pending ? ' is-streaming' : ''}`}>
                        {turn.answer ? withCitations(turn.text, turn.answer.sources, (m) => cite(turn.id, m)) : turn.text}
                      </div>
                    )}

                    {turn.answer && <AnswerNotices answer={turn.answer} />}
                    {turn.answer && <ComparisonBlock answer={turn.answer} />}
                    {turn.answer && !turn.pending && <TurnActions answer={turn.answer} />}

                    {isCompact && turn.answer && turn.answer.sources.length > 0 && (
                      <div className="chat-inline-sources" id={`${turn.id}-sources`}>
                        <p className="chat-inline-sources__title">
                          Sources (<span className="tabular-nums">{turn.answer.sources.length}</span>)
                        </p>
                        <ol className="sources-list">
                          {turn.answer.sources.map((s) => (
                            <SourceCard
                              key={`${turn.id}-${s.marker}`}
                              source={s}
                              turnId={turn.id}
                              highlighted={highlight?.turn === turn.id && highlight.marker === s.marker}
                            />
                          ))}
                        </ol>
                      </div>
                    )}

                    {turn.error && (
                      <div className="inline-error chat-error" role="alert">
                        <AlertTriangle size={15} aria-hidden="true" />
                        <span>{turn.error}</span>
                        <button
                          type="button"
                          className="link-button"
                          onClick={() => void send(turn.question, turn.id)}
                          disabled={busy}
                        >
                          <RotateCcw size={13} aria-hidden="true" /> Try again
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </article>
              );
            })}
          </div>

          {showJumpToBottom && (
            <button
              type="button"
              className="jump-bottom"
              onClick={() => {
                isAtBottomRef.current = true;
                setShowJumpToBottom(false);
                scrollToBottom();
              }}
            >
              <ArrowDown size={14} aria-hidden="true" /> Jump to latest
            </button>
          )}
        </div>

        <form
          className="chat-composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send(draft);
          }}
        >
          <div className="chat-reading-col">
            <div className="chat-scope" role="group" aria-label="Search in">
              <label className={includePrivate ? 'is-on' : ''}>
                <input type="checkbox" checked={includePrivate} onChange={(e) => setIncludePrivate(e.target.checked)} />
                <Lock size={13} aria-hidden="true" /> My private files
              </label>
              <label className={includePublic ? 'is-on' : ''}>
                <input type="checkbox" checked={includePublic} onChange={(e) => setIncludePublic(e.target.checked)} />
                <Globe size={13} aria-hidden="true" /> Shared documents
              </label>
              {noScope && <span className="chat-scope__hint">Pick at least one place to search.</span>}
            </div>

            <div className="chat-input">
              <textarea
                ref={inputRef}
                rows={1}
                value={draft}
                maxLength={QUESTION_MAX}
                placeholder="Ask a question about your documents…"
                aria-label="Your question"
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={onKeyDown}
              />
              {busy ? (
                <button
                  type="button"
                  className="chat-send chat-send--stop"
                  onClick={() => controller.current?.abort()}
                  aria-label="Stop answering"
                >
                  <Square size={15} />
                </button>
              ) : (
                <button
                  type="submit"
                  className="chat-send"
                  disabled={!draft.trim() || noScope || historyBusy}
                  aria-label="Send question"
                >
                  <ArrowUp size={18} />
                </button>
              )}
            </div>

            <div className="chat-composer__foot">
              <p className="chat-hint muted small">Enter to send · Shift + Enter for a new line</p>
              {draft.length > 3200 && (
                <span className="chat-char-count tabular-nums">
                  {draft.length} / {QUESTION_MAX}
                </span>
              )}
            </div>
          </div>
        </form>
      </section>

      {!isCompact && (
        <SourcesPanel
          turnId={activeSourceTurn?.id ?? null}
          question={activeSourceTurn?.question ?? null}
          sources={activeSourceTurn?.answer?.sources ?? []}
          insufficient={Boolean(activeSourceTurn?.answer?.insufficient)}
          pending={Boolean(activeSourceTurn?.pending && !activeSourceTurn?.answer)}
          highlightedMarker={
            activeSourceTurn && highlight?.turn === activeSourceTurn.id ? highlight.marker : null
          }
          turnOptions={turnOptions}
          onSelectTurn={(id) => {
            setSelectedSourceTurnId(id);
            setHighlight(null);
          }}
        />
      )}
    </div>
  );
}
