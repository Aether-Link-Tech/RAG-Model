import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AxiosError } from 'axios';

vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  default: { get: vi.fn() },
}));

import api from './api';
import { fetchUserHealth, toUserHealth } from './adminService';

const get = vi.mocked(api.get);
const httpError = (status: number) => Object.assign(new AxiosError('Request failed'), { response: { status } });

beforeEach(() => get.mockReset());

describe('toUserHealth', () => {
  it('maps the health endpoint row', () => {
    expect(
      toUserHealth({
        id: 'u1', email: 'a@example.com', full_name: 'A', role: 'user', status: 'quarantined',
        document_count: 3, storage_bytes: 2048, last_activity: '2026-11-20T10:00:00Z',
      }),
    ).toEqual({
      id: 'u1', email: 'a@example.com', full_name: 'A', role: 'user', status: 'quarantined', quarantined: true,
      document_count: 3, storage_bytes: 2048, last_activity: '2026-11-20T10:00:00Z',
    });
  });

  it('accepts alternative field names and missing values', () => {
    const row = toUserHealth({ id: 'u2', email: 'b@example.com', is_quarantined: true, storage_used_bytes: 10, last_activity_at: null });
    expect(row.quarantined).toBe(true);
    expect(row.storage_bytes).toBe(10);
    expect(row.document_count).toBeNull();
    expect(row.last_activity).toBeNull();
    expect(row.role).toBe('user');
  });
});

describe('fetchUserHealth', () => {
  it('reads every page of the health endpoint', async () => {
    const page = (ids: string[], total: number) => ({
      data: { items: ids.map((id) => ({ id, email: `${id}@example.com`, status: 'active' })), total, limit: 200, offset: 0 },
    });
    get.mockResolvedValueOnce(page(Array.from({ length: 200 }, (_, i) => `u${i}`), 201)).mockResolvedValueOnce(page(['last'], 201));

    const report = await fetchUserHealth();

    expect(report.metricsAvailable).toBe(true);
    expect(report.users).toHaveLength(201);
    expect(get).toHaveBeenNthCalledWith(2, '/admin/users/health', { params: { limit: 200, offset: 200 } });
  });

  it('falls back to the account list when the health endpoint is not deployed', async () => {
    get.mockRejectedValueOnce(httpError(404)).mockResolvedValueOnce({
      data: [
        { id: 'u1', email: 'a@example.com', role: 'admin', created_at: '', is_deleted: false },
        { id: 'u2', email: 'gone@example.com', role: 'user', created_at: '', is_deleted: true },
      ],
    });

    const report = await fetchUserHealth();

    expect(get).toHaveBeenLastCalledWith('/admin/users');
    expect(report.metricsAvailable).toBe(false);
    expect(report.users).toEqual([
      expect.objectContaining({ id: 'u1', role: 'admin', status: null, storage_bytes: null, document_count: null }),
    ]);
  });

  it('does not hide other errors behind the fallback', async () => {
    get.mockRejectedValueOnce(httpError(403));
    await expect(fetchUserHealth()).rejects.toBeInstanceOf(AxiosError);
    expect(get).toHaveBeenCalledTimes(1);
  });
});
