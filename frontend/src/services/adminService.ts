// FR-069 admin audit log viewer: GET /api/v1/admin/audit-log (admins only)
import api, { hasStatus } from './api';
import type { AuditEventType, AuditLogPage, User, UserHealth, UserHealthReport } from '../types';

export interface AuditQuery {
  eventType?: AuditEventType | '';
  q?: string;
  since?: string; // yyyy-mm-dd
  until?: string;
  limit?: number;
  offset?: number;
}

export async function fetchAuditLog({ eventType, q, since, until, limit = 25, offset = 0 }: AuditQuery): Promise<AuditLogPage> {
  const params: Record<string, string | number> = { limit, offset };
  if (eventType) params.event_type = eventType;
  if (q?.trim()) params.q = q.trim();
  if (since) params.since = new Date(`${since}T00:00:00`).toISOString();
  if (until) params.until = new Date(`${until}T23:59:59`).toISOString();
  const { data } = await api.get<AuditLogPage>('/admin/audit-log', { params });
  return data;
}

// Landing page: admins choose which public documents guests can ask about.
export async function setShowcase(documentId: string, showcase: boolean): Promise<{ id: string; showcase: boolean }> {
  const { data } = await api.patch<{ id: string; filename: string; showcase: boolean }>(
    `/admin/documents/${documentId}/showcase`,
    { showcase },
  );
  return data;
}

// ---------- FR-076 per-user health dashboard (AL-273) ----------

const HEALTH_PAGE_SIZE = 200;
const HEALTH_MAX_PAGES = 50; // safety stop: 10,000 accounts

type RawHealth = Record<string, unknown>;
type RawHealthPage = { items?: RawHealth[]; total?: number } | RawHealth[];

const num = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null);
const str = (value: unknown): string | null => (typeof value === 'string' && value ? value : null);

// Maps one row of GET /admin/users/health (AL-272) onto UserHealth.
// Accepts the alternative field names discussed for the endpoint so the page keeps working if they change.
export function toUserHealth(raw: RawHealth): UserHealth {
  const status = str(raw.status);
  const quarantined = raw.quarantined ?? raw.is_quarantined;
  return {
    id: String(raw.id),
    email: String(raw.email ?? ''),
    full_name: str(raw.full_name),
    role: str(raw.role) ?? 'user',
    status: status === 'quarantined' || status === 'active' ? status : null,
    quarantined: typeof quarantined === 'boolean' ? quarantined : status === 'quarantined',
    document_count: num(raw.document_count ?? raw.documents),
    storage_bytes: num(raw.storage_bytes ?? raw.storage_used_bytes ?? raw.storage_used),
    last_activity: str(raw.last_activity ?? raw.last_activity_at),
  };
}

// Accounts only, used until the health endpoint is deployed: no metrics or quarantine data.
function fromAccount(user: User): UserHealth {
  return {
    id: user.id,
    email: user.email,
    full_name: user.full_name ?? null,
    role: user.role,
    status: null,
    quarantined: false,
    document_count: null,
    storage_bytes: null,
    last_activity: null,
  };
}

/** Loads every user's health row (all pages); filtering and sorting happen in the page. */
export async function fetchUserHealth(): Promise<UserHealthReport> {
  try {
    const users: UserHealth[] = [];
    for (let page = 0; page < HEALTH_MAX_PAGES; page++) {
      const { data } = await api.get<RawHealthPage>('/admin/users/health', {
        params: { limit: HEALTH_PAGE_SIZE, offset: page * HEALTH_PAGE_SIZE },
      });
      const items = Array.isArray(data) ? data : (data.items ?? []);
      users.push(...items.map(toUserHealth));
      const total = Array.isArray(data) ? items.length : (data.total ?? users.length);
      if (Array.isArray(data) || items.length === 0 || users.length >= total) break;
    }
    return { users, metricsAvailable: true };
  } catch (error) {
    if (!hasStatus(error, 404)) throw error;
    const { data } = await api.get<User[]>('/admin/users');
    return { users: data.filter((user) => !user.is_deleted).map(fromAccount), metricsAvailable: false };
  }
}
