// /admin/users — per-user health dashboard for admins (FR-076 / AL-273)
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  FileStack,
  HardDrive,
  Info,
  TriangleAlert,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  Users,
} from 'lucide-react';
import { fetchUserHealth } from '../services/adminService';
import { getErrorMessage } from '../services/api';
import { formatDate, formatMegabytes, relativeTime } from '../utils/format';
import type { UserHealth, UserHealthReport } from '../types';

const PAGE_SIZE = 25;

type SortKey = 'user' | 'status' | 'documents' | 'storage' | 'activity';
type SortDir = 'asc' | 'desc';
type StatusFilter = 'all' | 'active' | 'quarantined';
type RoleFilter = 'all' | 'admin' | 'user';

// Two letters for the avatar: from the name when known, otherwise from the email's letters.
function initialsOf(user: UserHealth): string {
  const words = (user.full_name ?? '').trim().split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return (user.full_name ?? user.email).replace(/[^a-z]/gi, '').slice(0, 2).toUpperCase() || '?';
}

const COLUMNS: { key: SortKey; label: string; numeric?: boolean }[] = [
  { key: 'user', label: 'User' },
  { key: 'status', label: 'Status' },
  { key: 'documents', label: 'Documents', numeric: true },
  { key: 'storage', label: 'Storage', numeric: true },
  { key: 'activity', label: 'Last activity' },
];

// Value each column sorts by; null means "no data" and always sorts last.
function sortValue(row: UserHealth, key: SortKey): string | number | null {
  switch (key) {
    case 'user':
      return row.email.toLowerCase();
    case 'status':
      if (row.quarantined) return 0;
      return row.status === 'active' ? 1 : null;
    case 'documents':
      return row.document_count;
    case 'storage':
      return row.storage_bytes;
    case 'activity':
      return row.last_activity ? new Date(row.last_activity).getTime() : null;
  }
}

function compareRows(a: UserHealth, b: UserHealth, key: SortKey, dir: SortDir): number {
  const va = sortValue(a, key);
  const vb = sortValue(b, key);
  if (va === null || vb === null) {
    if (va !== vb) return va === null ? 1 : -1;
  } else if (va !== vb) {
    const order = va < vb ? -1 : 1;
    return dir === 'asc' ? order : -order;
  }
  return a.email.localeCompare(b.email);
}

export default function UserHealthPage() {
  const [report, setReport] = useState<UserHealthReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [loadedAt, setLoadedAt] = useState<string | null>(null);

  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [roleFilter, setRoleFilter] = useState<RoleFilter>('all');
  // Quarantined accounts come first by default so they're seen straight away.
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: 'status', dir: 'asc' });
  const [page, setPage] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setReport(await fetchUserHealth());
      setLoadedAt(new Date().toISOString());
    } catch (err) {
      setError(getErrorMessage(err, 'Could not load user health.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Any change to the filters or sort starts again from the first page.
  useEffect(() => setPage(0), [search, statusFilter, roleFilter, sort]);

  const users = report?.users ?? [];
  const metrics = report?.metricsAvailable ?? true;

  const visible = useMemo(() => {
    const term = search.trim().toLowerCase();
    return users
      .filter((u) => !term || u.email.toLowerCase().includes(term) || (u.full_name ?? '').toLowerCase().includes(term))
      .filter((u) => statusFilter === 'all' || (statusFilter === 'quarantined' ? u.quarantined : !u.quarantined && u.status === 'active'))
      .filter((u) => roleFilter === 'all' || u.role === roleFilter)
      .sort((a, b) => compareRows(a, b, sort.key, sort.dir));
  }, [users, search, statusFilter, roleFilter, sort]);

  const totals = useMemo(
    () => ({
      quarantined: users.filter((u) => u.quarantined).length,
      documents: users.reduce((sum, u) => sum + (u.document_count ?? 0), 0),
      storage: users.reduce((sum, u) => sum + (u.storage_bytes ?? 0), 0),
      maxStorage: Math.max(0, ...users.map((u) => u.storage_bytes ?? 0)),
    }),
    [users],
  );

  function toggleSort(key: SortKey, numeric?: boolean) {
    setSort((current) =>
      current.key === key
        ? { key, dir: current.dir === 'asc' ? 'desc' : 'asc' }
        : { key, dir: numeric || key === 'activity' ? 'desc' : 'asc' },
    );
  }

  const stats = [
    { label: 'Users', value: users.length.toLocaleString(), icon: Users, tone: 'blue' },
    { label: 'Quarantined', value: metrics ? totals.quarantined.toLocaleString() : '–', icon: ShieldAlert, tone: 'red' },
    { label: 'Documents', value: metrics ? totals.documents.toLocaleString() : '–', icon: FileStack, tone: 'navy' },
    { label: 'Storage used', value: metrics ? formatMegabytes(totals.storage) : '–', icon: HardDrive, tone: 'green' },
  ];

  const total = visible.length;
  const from = total ? page * PAGE_SIZE + 1 : 0;
  const to = Math.min((page + 1) * PAGE_SIZE, total);
  const rows = visible.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const filtered = Boolean(search.trim()) || statusFilter !== 'all' || roleFilter !== 'all';

  return (
    <div className="page health-page">
      <section className="page-hero fade-up">
        <div>
          <span className="eyebrow">
            <Activity size={14} /> Administration
          </span>
          <h1>User health</h1>
          <p className="muted">
            Account status, documents and storage for every user. Quarantined accounts are highlighted.
          </p>
        </div>
        <div className="health-hero__actions">
          {loadedAt && !loading && <span className="muted small">Updated {relativeTime(loadedAt)}</span>}
          <button type="button" className="button button--ghost" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={16} className={loading ? 'spin' : ''} /> Refresh
          </button>
        </div>
      </section>

      <section className="stat-row" aria-label="Summary">
        {stats.map(({ label, value, icon: Icon, tone }, i) => (
          <div key={label} className={`stat-card stat-card--${tone}`} style={{ animationDelay: `${80 + i * 70}ms` }}>
            <span className="stat-card__icon">
              <Icon size={20} />
            </span>
            <div>
              <strong data-testid={`stat-${label}`}>{report ? value : '–'}</strong>
              <span>{label}</span>
            </div>
          </div>
        ))}
      </section>

      <section className="panel health-panel card-in" aria-label="User health">
        <div className="health-filters">
          <label className="search">
            <Search size={16} />
            <input
              type="search"
              placeholder="Search by email or name"
              aria-label="Search users"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <select
            aria-label="Status"
            value={statusFilter}
            disabled={!metrics}
            onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
          >
            <option value="all">All statuses</option>
            <option value="active">Active</option>
            <option value="quarantined">Quarantined</option>
          </select>
          <select aria-label="Role" value={roleFilter} onChange={(e) => setRoleFilter(e.target.value as RoleFilter)}>
            <option value="all">All roles</option>
            <option value="admin">Admins</option>
            <option value="user">Users</option>
          </select>
        </div>

        {!metrics && (
          <div className="health-notice" role="status">
            <Info size={16} />
            <span>
              Showing accounts only. Status, documents, storage and activity appear once the health endpoint
              (<code>GET /admin/users/health</code>, AL-272) is available.
            </span>
          </div>
        )}

        {error && (
          <div className="health-notice health-notice--error" role="alert">
            <TriangleAlert size={16} />
            <span>{error}</span>
          </div>
        )}

        {loading && !report ? (
          <div className="audit-skeleton" aria-busy="true">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="skeleton skeleton--row" />
            ))}
          </div>
        ) : report && rows.length === 0 ? (
          <div className="empty-state">
            <span className="empty-state__icon">
              <ShieldCheck size={28} />
            </span>
            <h3>{filtered ? 'No matching users' : 'No users yet'}</h3>
            <p>{filtered ? 'Try another search or clear the filters.' : 'Accounts appear here once people sign up.'}</p>
          </div>
        ) : report ? (
          <div className="table-scroll">
            <table className="audit-table health-table">
              <thead>
                <tr>
                  {COLUMNS.map(({ key, label, numeric }) => {
                    const active = sort.key === key;
                    const SortIcon = active ? (sort.dir === 'asc' ? ArrowUp : ArrowDown) : ArrowUpDown;
                    return (
                      <th
                        key={key}
                        scope="col"
                        className={numeric ? 'is-numeric' : undefined}
                        aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
                      >
                        <button
                          type="button"
                          className={`sort-button${active ? ' is-active' : ''}`}
                          onClick={() => toggleSort(key, numeric)}
                        >
                          {label} <SortIcon size={13} aria-hidden="true" />
                        </button>
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {rows.map((user, index) => (
                  <tr
                    key={user.id}
                    data-testid="health-row"
                    className={user.quarantined ? 'is-quarantined' : undefined}
                    style={{ animationDelay: `${Math.min(index, 12) * 30}ms` }}
                  >
                    <td>
                      <div className="health-user">
                        <span className="health-user__avatar" aria-hidden="true">
                          {initialsOf(user)}
                        </span>
                        <div>
                          <span className="health-user__email" title={user.email}>
                            {user.email}
                          </span>
                          <small>
                            {user.full_name ?? 'No name'} · {user.role === 'admin' ? 'Admin' : 'User'}
                          </small>
                        </div>
                      </div>
                    </td>
                    <td>
                      {user.quarantined ? (
                        <span className="badge badge--failed">
                          <ShieldAlert size={12} /> Quarantined
                        </span>
                      ) : user.status === 'active' ? (
                        <span className="badge badge--indexed">Active</span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="is-numeric">{user.document_count?.toLocaleString() ?? '—'}</td>
                    <td className="is-numeric">
                      {user.storage_bytes === null ? (
                        '—'
                      ) : (
                        <div className="storage-cell">
                          <span>{formatMegabytes(user.storage_bytes)}</span>
                          <span className="storage-bar" aria-hidden="true">
                            <span style={{ width: `${totals.maxStorage ? (user.storage_bytes / totals.maxStorage) * 100 : 0}%` }} />
                          </span>
                        </div>
                      )}
                    </td>
                    <td>
                      {user.last_activity ? (
                        <span title={formatDate(user.last_activity)}>
                          <span className="audit-when">{relativeTime(user.last_activity)}</span>
                          <small>{formatDate(user.last_activity)}</small>
                        </span>
                      ) : (
                        <span className="muted">{metrics ? 'Never' : '—'}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {report && total > 0 && (
          <footer className="audit-pager">
            <span>
              {from}–{to} of {total} {total === 1 ? 'user' : 'users'}
            </span>
            <div>
              <button
                type="button"
                className="icon-button"
                aria-label="Previous page"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                <ChevronLeft size={18} />
              </button>
              <button
                type="button"
                className="icon-button"
                aria-label="Next page"
                disabled={to >= total}
                onClick={() => setPage((p) => p + 1)}
              >
                <ChevronRight size={18} />
              </button>
            </div>
          </footer>
        )}
      </section>
    </div>
  );
}
