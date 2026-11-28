import { useRef } from 'react';
import { Link } from 'react-router-dom';
import { FileText, Folder, Globe, Lock, MessageSquare, MessageSquarePlus, X } from 'lucide-react';
import { useFocusTrap } from '../hooks/useFocusTrap';
import type { Collection, Document } from '../types';

export interface SessionThread {
  id: string;
  title: string;
  turnCount: number;
  active: boolean;
}

interface ChatSidebarProps {
  openMobile: boolean;
  onCloseMobile: () => void;
  threads: SessionThread[];
  conversationsLoading?: boolean;
  conversationsError?: string | null;
  onRetryConversations?: () => void;
  onSelectThread: (id: string) => void;
  onNewConversation: () => void;
  canStartNew: boolean;
  docs: Document[] | null;
  collections: Collection[];
}

export default function ChatSidebar({
  openMobile,
  onCloseMobile,
  threads,
  conversationsLoading = false,
  conversationsError = null,
  onRetryConversations,
  onSelectThread,
  onNewConversation,
  canStartNew,
  docs,
  collections,
}: ChatSidebarProps) {
  const drawerRef = useRef<HTMLElement>(null);
  useFocusTrap(drawerRef, openMobile, onCloseMobile);

  const indexed = docs ? docs.filter((d) => d.status === 'indexed') : [];
  const privateCount = indexed.filter((d) => d.visibility === 'private').length;
  const publicCount = indexed.filter((d) => d.visibility === 'public').length;

  const content = (
    <div className="chat-sidebar__inner">
      <div className="chat-sidebar__top">
        <button
          type="button"
          className="button button--primary button--wide"
          onClick={() => {
            onNewConversation();
            onCloseMobile();
          }}
          disabled={!canStartNew}
        >
          <MessageSquarePlus size={16} aria-hidden="true" /> New conversation
        </button>
        {openMobile && (
          <button
            type="button"
            className="icon-button chat-sidebar__close"
            aria-label="Close sidebar"
            onClick={onCloseMobile}
          >
            <X size={18} />
          </button>
        )}
      </div>

      <section className="chat-sidebar__section" aria-label="Conversations">
        <div className="chat-sidebar__label">
          <span>Conversations</span>
        </div>
        {conversationsError && (
          <div className="chat-sidebar__error" role="alert">
            <span>{conversationsError}</span>
            {onRetryConversations && (
              <button type="button" className="chat-sidebar__manage" onClick={onRetryConversations}>
                Retry
              </button>
            )}
          </div>
        )}
        {conversationsLoading && threads.length === 0 ? (
          <div className="chat-sidebar__skeleton" aria-busy="true" aria-label="Loading conversations">
            <span className="skeleton skeleton--line" />
            <span className="skeleton skeleton--line skeleton--short" />
          </div>
        ) : threads.length === 0 ? (
          !conversationsError && <p className="chat-sidebar__empty">No conversations yet.</p>
        ) : (
          <ul className="thread-list">
            {threads.map((t) => (
              <li key={t.id}>
                <button
                  type="button"
                  className={`thread-item${t.active ? ' is-active' : ''}`}
                  aria-current={t.active ? 'true' : undefined}
                  onClick={() => {
                    onSelectThread(t.id);
                    onCloseMobile();
                  }}
                >
                  <MessageSquare size={15} aria-hidden="true" />
                  <span className="thread-item__title" title={t.title}>
                    {t.title}
                  </span>
                  <span className="thread-item__count tabular-nums">{t.turnCount}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="chat-sidebar__section" aria-label="Knowledge base">
        <div className="chat-sidebar__label">
          <span>Knowledge base</span>
          <Link to="/documents" className="chat-sidebar__manage">
            Manage
          </Link>
        </div>

        {docs === null ? (
          <div className="chat-sidebar__skeleton" aria-busy="true">
            <span className="skeleton skeleton--line" />
            <span className="skeleton skeleton--line skeleton--short" />
          </div>
        ) : (
          <>
            <div className="kb-summary">
              <div className="kb-summary__row">
                <span>
                  <FileText size={14} aria-hidden="true" /> Ready to search
                </span>
                <strong className="tabular-nums">{indexed.length}</strong>
              </div>
              <div className="kb-summary__row">
                <span>
                  <Lock size={14} aria-hidden="true" /> Private
                </span>
                <span className="tabular-nums">{privateCount}</span>
              </div>
              <div className="kb-summary__row">
                <span>
                  <Globe size={14} aria-hidden="true" /> Shared
                </span>
                <span className="tabular-nums">{publicCount}</span>
              </div>
            </div>

            {collections.length > 0 && (
              <ul className="kb-collections" aria-label="Document collections">
                {collections.map((c) => (
                  <li key={c.id} className="kb-collections__item">
                    <Folder size={13} aria-hidden="true" />
                    <span title={c.name}>{c.name}</span>
                    <small className="tabular-nums">{c.document_count}</small>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>
    </div>
  );

  return (
    <>
      {openMobile && (
        <div className="sidebar-backdrop" onClick={onCloseMobile} aria-hidden="true" />
      )}
      <aside
        ref={drawerRef}
        className={`chat-sidebar${openMobile ? ' is-open' : ''}`}
        aria-label="Conversations and documents"
        aria-modal={openMobile || undefined}
        role={openMobile ? 'dialog' : undefined}
        tabIndex={openMobile ? -1 : undefined}
      >
        {content}
      </aside>
    </>
  );
}
