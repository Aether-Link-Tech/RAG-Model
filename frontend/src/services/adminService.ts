// FR-069 admin audit log viewer: GET /api/v1/admin/audit-log (admins only)
import api from './api';
import type { AuditEventType, AuditLogPage } from '../types';

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
