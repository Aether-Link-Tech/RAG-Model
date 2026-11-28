import { useState } from 'react';
import { BookOpen, Check, ChevronDown, ChevronUp, Copy, FileText, ScanText } from 'lucide-react';
import type { ChatSource } from '../types';

/** Highlights the supporting sentence inside an excerpt when available. */
export function HighlightedExcerpt({ text, highlight }: { text: string; highlight?: string | null }) {
  const index = highlight ? text.indexOf(highlight) : -1;
  if (!highlight || index < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, index)}
      <mark className="source-highlight">{highlight}</mark>
      {text.slice(index + highlight.length)}
    </>
  );
}

/** Formats cited sources into plain text for clipboard copy. */
export function formatSourcesText(sources: ChatSource[]): string {
  return sources
    .map((s) => {
      const meta = [s.page != null ? `p. ${s.page}` : null, s.section || null].filter(Boolean).join(', ');
      return `[${s.marker}] ${s.document_name}${meta ? ` (${meta})` : ''}\n${s.highlight || s.text}`;
    })
    .join('\n\n');
}

export function SourceCard({
  source,
  highlighted,
  turnId,
}: {
  source: ChatSource;
  highlighted: boolean;
  turnId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const isOpen = expanded || highlighted;
  const pct = Math.round(Math.max(0, Math.min(1, source.score)) * 100);

  return (
    <li
      className={`source-card${highlighted ? ' is-highlighted' : ''}`}
      id={`source-${source.marker}`}
      data-turn-source={`${turnId}-${source.marker}`}
    >
      <button
        type="button"
        className="source-card__head"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={isOpen}
      >
        <span className="source-card__marker tabular-nums">{source.marker}</span>
        <span className="source-card__doc">
          <span className="source-card__name" title={source.document_name}>
            <FileText size={14} aria-hidden="true" />
            <span>{source.document_name}</span>
          </span>
          {(source.page != null || source.section || source.source_type === 'ocr') && (
            <span className="source-card__details">
              {source.page != null && <span className="tabular-nums">p. {source.page}</span>}
              {source.section && <span className="source-card__section-tag">{source.section}</span>}
              {source.source_type === 'ocr' && (
                <span className="source-card__ocr" title="Text extracted from an image via OCR">
                  <ScanText size={11} aria-hidden="true" /> OCR
                </span>
              )}
            </span>
          )}
        </span>
        <span className="source-card__right">
          <span className="source-card__score-wrap" title={`Relevance ${pct}%`}>
            <span className="source-card__score-num tabular-nums">{pct}%</span>
            <span className="source-card__score" aria-hidden="true">
              <i style={{ width: `${pct}%` }} />
            </span>
          </span>
          {isOpen ? <ChevronUp size={15} aria-hidden="true" /> : <ChevronDown size={15} aria-hidden="true" />}
        </span>
      </button>

      <div className={`source-card__body${isOpen ? ' is-open' : ''}`}>
        <p className="source-card__text">
          <HighlightedExcerpt text={source.text} highlight={source.highlight} />
        </p>
      </div>
    </li>
  );
}

interface SourcesPanelProps {
  turnId: string | null;
  question: string | null;
  sources: ChatSource[];
  insufficient: boolean;
  pending: boolean;
  highlightedMarker: number | null;
  turnOptions?: { id: string; index: number; question: string; sourceCount: number }[];
  onSelectTurn?: (turnId: string) => void;
}

export default function SourcesPanel({
  turnId,
  question,
  sources,
  insufficient,
  pending,
  highlightedMarker,
  turnOptions = [],
  onSelectTurn,
}: SourcesPanelProps) {
  const [copied, setCopied] = useState(false);

  async function copyAll() {
    if (!sources.length) return;
    try {
      await navigator.clipboard?.writeText(formatSourcesText(sources));
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard permission denied */
    }
  }

  return (
    <aside className="sources-panel" aria-label="Retrieved sources">
      <div className="sources-panel__head">
        <div className="sources-panel__title">
          <h2>Sources</h2>
          {sources.length > 0 && <span className="sources-panel__count tabular-nums">{sources.length}</span>}
        </div>
        {sources.length > 0 && (
          <button type="button" className="button button--ghost button--sm" onClick={copyAll}>
            {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
            <span>{copied ? 'Copied' : 'Copy sources'}</span>
          </button>
        )}
      </div>

      {turnOptions.length > 1 && onSelectTurn && turnId && (
        <div className="sources-panel__turns" aria-label="Select answer sources">
          <label className="sr-only" htmlFor="sources-turn-select">
            Answer
          </label>
          <select
            id="sources-turn-select"
            value={turnId}
            onChange={(e) => onSelectTurn(e.target.value)}
          >
            {turnOptions.map((opt) => (
              <option key={opt.id} value={opt.id}>
                Q{opt.index}: {opt.question.slice(0, 48)}{opt.question.length > 48 ? '…' : ''} ({opt.sourceCount})
              </option>
            ))}
          </select>
        </div>
      )}

      {question && (
        <p className="sources-panel__context" title={question}>
          For: “{question}”
        </p>
      )}

      <div className="sources-panel__body">
        {pending && sources.length === 0 ? (
          <ul className="sources-skeleton" aria-busy="true" aria-label="Loading sources">
            {[0, 1, 2].map((i) => (
              <li key={i} className="sources-skeleton__card">
                <span className="skeleton skeleton--line" />
                <span className="skeleton skeleton--line skeleton--short" />
              </li>
            ))}
          </ul>
        ) : sources.length > 0 && turnId ? (
          <ol className="sources-list" id={`${turnId}-sources`}>
            {sources.map((s) => (
              <SourceCard
                key={`${turnId}-${s.marker}`}
                source={s}
                turnId={turnId}
                highlighted={highlightedMarker === s.marker}
              />
            ))}
          </ol>
        ) : insufficient ? (
          <div className="sources-empty">
            <BookOpen size={20} aria-hidden="true" />
            <strong>No matching passages</strong>
            <p>None of your documents reached the evidence threshold for this question.</p>
          </div>
        ) : (
          <div className="sources-empty">
            <BookOpen size={20} aria-hidden="true" />
            <strong>Citations appear here</strong>
            <p>Ask a question to inspect retrieved passages, document names, page numbers, and scores.</p>
          </div>
        )}
      </div>
    </aside>
  );
}
