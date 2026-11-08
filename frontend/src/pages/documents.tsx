// Documents workspace (/documents): list + detail (FR-009), upload (FR-008/077),
// rename/replace (FR-010), delete (FR-011), rejection UX (FR-068).
import { useCallback, useEffect, useMemo, useState } from 'react';
import { CircleAlert, FileStack, Globe, Lock, RefreshCw, Search, Sparkles } from 'lucide-react';
import UploadPanel from '../components/UploadPanel';
import DocumentDrawer from '../components/DocumentDrawer';
import FileIcon from '../components/FileIcon';
import { StatusBadge, VisibilityBadge } from '../components/Badges';
import { useToast } from '../components/Toast';
import { useSession } from '../hooks/useSession';
import { listDocuments } from '../services/documentService';
import { getErrorMessage } from '../services/api';
import { formatBytes, relativeTime } from '../utils/format';
import type { Document } from '../types';

type Filter = 'all' | 'mine' | 'shared' | 'failed';

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'mine', label: 'My documents' },
  { id: 'shared', label: 'Shared with everyone' },
  { id: 'failed', label: 'Needs attention' },
];

export default function DocumentsPage() {
  const toast = useToast();
  const { user } = useSession();
  const [docs, setDocs] = useState<Document[] | null>(null);
  const [loadError, setLoadError] = useState('');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      setDocs(await listDocuments());
      setLoadError('');
    } catch (error) {
      setLoadError(getErrorMessage(error, 'Could not load your documents.'));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Keep polling while anything is still being processed.
  const busyCount = docs?.filter((d) => d.status === 'pending' || d.status === 'processing').length ?? 0;
  useEffect(() => {
    if (!busyCount) return;
    const timer = setInterval(() => void load(true), 3000);
    return () => clearInterval(timer);
  }, [busyCount, load]);

  const stats = useMemo(() => {
    const list = docs ?? [];
    return {
      total: list.length,
      privateCount: list.filter((d) => d.visibility === 'private').length,
      publicCount: list.filter((d) => d.visibility === 'public').length,
      failed: list.filter((d) => d.status === 'failed').length,
    };
  }, [docs]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (docs ?? []).filter((d) => {
      if (q && !d.filename.toLowerCase().includes(q)) return false;
      if (filter === 'mine') return d.is_owner;
      if (filter === 'shared') return d.visibility === 'public';
      if (filter === 'failed') return d.status === 'failed';
      return true;
    });
  }, [docs, query, filter]);

  const selected = docs?.find((d) => d.id === selectedId) ?? null;
  const canDelete = (d: Document) => d.is_owner || (d.visibility === 'public' && user?.role === 'admin');

  function upsert(doc: Document) {
    setDocs((list) => {
      const rest = (list ?? []).filter((d) => d.id !== doc.id);
      return [doc, ...rest];
    });
  }

  return (
    <div className="documents-page">
      <section className="page-hero fade-up">
        <div>
          <span className="eyebrow">
            <Sparkles size={14} /> Knowledge base
          </span>
          <h1>Your documents</h1>
          <p className="muted">Upload files to make them searchable. Private files are visible only to you.</p>
        </div>
        <button type="button" className="button button--ghost" onClick={() => void load()} disabled={refreshing}>
          <RefreshCw size={16} className={refreshing ? 'spin' : ''} /> Refresh
        </button>
      </section>

      <section className="stat-row">
        {[
          { label: 'Documents', value: stats.total, Icon: FileStack, tone: 'blue' },
          { label: 'Private', value: stats.privateCount, Icon: Lock, tone: 'navy' },
          { label: 'Public', value: stats.publicCount, Icon: Globe, tone: 'green' },
          { label: 'Needs attention', value: stats.failed, Icon: CircleAlert, tone: 'red' },
        ].map(({ label, value, Icon, tone }, i) => (
          <div key={label} className={`stat-card stat-card--${tone}`} style={{ animationDelay: `${80 + i * 70}ms` }}>
            <span className="stat-card__icon">
              <Icon size={18} />
            </span>
            <div>
              <strong data-testid={`stat-${label}`}>{docs ? value : '–'}</strong>
              <span>{label}</span>
            </div>
          </div>
        ))}
      </section>

      <div className="documents-grid">
        <section className="panel list-panel">
          <div className="list-toolbar">
            <label className="search">
              <Search size={16} />
              <input placeholder="Search by file name" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search documents" />
            </label>
            <div className="tabs" role="tablist">
              {FILTERS.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  role="tab"
                  aria-selected={filter === f.id}
                  className={filter === f.id ? 'is-active' : ''}
                  onClick={() => setFilter(f.id)}
                >
                  {f.label}
                </button>
              ))}
            </div>
          </div>

          {loadError && (
            <div className="inline-error" role="alert">
              <CircleAlert size={17} /> {loadError}
              <button type="button" className="link-button" onClick={() => void load()}>
                Try again
              </button>
            </div>
          )}

          {!docs && !loadError && (
            <ul className="doc-list" aria-busy="true">
              {[0, 1, 2, 3].map((i) => (
                <li key={i} className="doc-row doc-row--skeleton">
                  <span className="skeleton skeleton--icon" />
                  <span className="skeleton skeleton--line" />
                  <span className="skeleton skeleton--chip" />
                </li>
              ))}
            </ul>
          )}

          {docs && visible.length === 0 && (
            <div className="empty-state">
              <span className="empty-state__icon">
                <FileStack size={30} />
              </span>
              <strong>{docs.length === 0 ? 'No documents yet' : 'No documents match'}</strong>
              <span className="muted">
                {docs.length === 0 ? 'Upload your first PDF, DOCX or TXT file to get started.' : 'Try another search or filter.'}
              </span>
            </div>
          )}

          {docs && visible.length > 0 && (
            <ul className="doc-list">
              {visible.map((doc, i) => (
                <li key={doc.id} style={{ animationDelay: `${Math.min(i, 12) * 45}ms` }}>
                  <button
                    type="button"
                    className={`doc-row${selectedId === doc.id ? ' is-selected' : ''}`}
                    onClick={() => setSelectedId(doc.id)}
                    data-testid="doc-row"
                  >
                    <FileIcon filename={doc.filename} />
                    <span className="doc-row__main">
                      <strong title={doc.filename}>{doc.filename}</strong>
                      <span className="muted">
                        {formatBytes(doc.file_size)} · {relativeTime(doc.created_at)}
                        {!doc.is_owner && ' · shared'}
                      </span>
                    </span>
                    <span className="doc-row__badges">
                      <VisibilityBadge visibility={doc.visibility} isOwner={doc.is_owner} />
                      <StatusBadge status={doc.status} />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <UploadPanel
          onUploaded={(doc) => {
            upsert(doc);
            setFilter('all');
          }}
        />
      </div>

      <DocumentDrawer
        doc={selected}
        canDelete={selected ? canDelete(selected) : false}
        onClose={() => setSelectedId(null)}
        onChanged={(doc) => {
          upsert(doc);
          if (doc.status === 'processing') toast.info('Re-indexing', 'The document will be searchable again shortly.');
        }}
        onDeleted={(id) => {
          setSelectedId(null);
          setDocs((list) => (list ?? []).filter((d) => d.id !== id));
        }}
      />
    </div>
  );
}
