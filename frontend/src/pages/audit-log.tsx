// /admin/audit — security audit log viewer for admins (FR-069 / AL-168)
import { useCallback, useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, RefreshCw, Search, ShieldAlert, ShieldCheck } from 'lucide-react';
import { fetchAuditLog, type AuditQuery } from '../services/adminService';
import { getErrorMessage } from '../services/api';
import { formatDate, relativeTime } from '../utils/format';
import type { AuditEventType, AuditLogPage } from '../types';

const PAGE_SIZE = 25;

export const EVENT_LABELS: Record<AuditEventType, string> = {
  upload_rejected: 'Upload rejected',
  replace_rejected: 'Replacement rejected',
  access_denied: 'Access denied',
  download_blocked: 'Download blocked',
  document_published: 'Document published',
  ocr_image_skipped: 'OCR image skipped',
};

export default function AuditLogPage() {
  const [filters, setFilters] = useState<AuditQuery>({ eventType: '', q: '', since: '', until: '' });
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<AuditLogPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setPage(await fetchAuditLog({ ...filters, limit: PAGE_SIZE, offset }));
    } catch (err) {
      setError(getErrorMessage(err, 'Could not load the audit log.'));
    } finally {
      setLoading(false);
    }
  }, [filters, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  // Debounce free-text search
  useEffect(() => {
    const id = setTimeout(() => {
      setOffset(0);
      setFilters((f) => (f.q === search ? f : { ...f, q: search }));
    }, 350);
    return () => clearTimeout(id);
  }, [search]);

  function setFilter(patch: Partial<AuditQuery>) {
    setOffset(0);
    setFilters((f) => ({ ...f, ...patch }));
  }

  const total = page?.total ?? 0;
  const from = total ? offset + 1 : 0;
  const to = Math.min(offset + PAGE_SIZE, total);

  return (
    <div className="page audit-page">
      <section className="page-hero fade-up">
        <div>
          <span className="eyebrow">
            <ShieldAlert size={14} /> Security
          </span>
          <h1>Audit log</h1>
          <p className="muted">
            Rejected uploads, blocked downloads and denied actions. Entries are kept for 90 days.
          </p>
        </div>
        <button type="button" className="button button--ghost" onClick={() => void load()} disabled={loading}>
          <RefreshCw size={16} className={loading ? 'spin' : ''} /> Refresh
        </button>
      </section>

      <section className="panel audit-panel card-in" aria-label="Audit log">
        <div className="audit-filters">
          <label className="search">
            <Search size={16} />
            <input
              type="search"
              placeholder="Search file name or reason"
              aria-label="Search audit log"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <select
            aria-label="Event type"
            value={filters.eventType}
            onChange={(e) => setFilter({ eventType: e.target.value as AuditEventType | '' })}
          >
            <option value="">All events</option>
            {Object.entries(EVENT_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <label className="date-field">
            From
            <input type="date" aria-label="From date" value={filters.since} onChange={(e) => setFilter({ since: e.target.value })} />
          </label>
          <label className="date-field">
            To
            <input type="date" aria-label="To date" value={filters.until} onChange={(e) => setFilter({ until: e.target.value })} />
          </label>
        </div>

        {error && (
          <p className="alert alert--error" role="alert">
            {error}
          </p>
        )}

        {loading && !page ? (
          <div className="audit-skeleton" aria-busy="true">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="skeleton skeleton--row" />
            ))}
          </div>
        ) : page && page.items.length === 0 ? (
          <div className="empty-state">
            <span className="empty-state__icon">
              <ShieldCheck size={28} />
            </span>
            <h3>No security events</h3>
            <p>Nothing matches these filters. Rejected uploads and denied actions appear here.</p>
          </div>
        ) : (
          <div className="table-scroll">
            <table className="audit-table">
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Event</th>
                  <th scope="col">File</th>
                  <th scope="col">Reason</th>
                  <th scope="col">User</th>
                  <th scope="col">IP address</th>
                </tr>
              </thead>
              <tbody>
                {page?.items.map((entry, index) => (
                  <tr key={entry.id} data-testid="audit-row" style={{ animationDelay: `${Math.min(index, 12) * 30}ms` }}>
                    <td title={formatDate(entry.created_at)}>
                      <span className="audit-when">{relativeTime(entry.created_at)}</span>
                      <small>{formatDate(entry.created_at)}</small>
                    </td>
                    <td>
                      <span className={`event-badge event-badge--${entry.event_type}`}>
                        {EVENT_LABELS[entry.event_type] ?? entry.event_type}
                      </span>
                    </td>
                    <td className="audit-file">{entry.filename ?? '—'}</td>
                    <td className="audit-reason">{entry.reason}</td>
                    <td>
                      <code title={entry.user_id ?? undefined}>{entry.user_id ? entry.user_id.slice(0, 8) : '—'}</code>
                    </td>
                    <td>
                      <code>{entry.ip_address ?? '—'}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <footer className="audit-pager">
          <span>
            {from}–{to} of {total}
          </span>
          <div>
            <button
              type="button"
              className="icon-button"
              aria-label="Previous page"
              disabled={offset === 0 || loading}
              onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
            >
              <ChevronLeft size={18} />
            </button>
            <button
              type="button"
              className="icon-button"
              aria-label="Next page"
              disabled={to >= total || loading}
              onClick={() => setOffset((o) => o + PAGE_SIZE)}
            >
              <ChevronRight size={18} />
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
