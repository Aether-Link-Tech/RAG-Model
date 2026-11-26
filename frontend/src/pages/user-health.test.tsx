import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from '../App';
import { ToastProvider } from '../components/Toast';
import { session } from '../services/session';
import type { User, UserHealth } from '../types';

vi.mock('../services/adminService', () => ({
  fetchUserHealth: vi.fn(),
  fetchAuditLog: vi.fn(),
  setShowcase: vi.fn(),
}));
vi.mock('../services/documentService', () => ({
  listDocuments: vi.fn(async () => []),
}));

import * as admin from '../services/adminService';

const MB = 1024 * 1024;
const adminUser: User = { id: 'a', email: 'admin@example.com', role: 'admin', created_at: '', is_deleted: false };
const member: User = { id: 'm', email: 'member@example.com', role: 'user', created_at: '', is_deleted: false };

const row = (over: Partial<UserHealth>): UserHealth => ({
  id: 'x', email: 'x@example.com', full_name: null, role: 'user', status: 'active', quarantined: false,
  document_count: 0, storage_bytes: 0, last_activity: null, ...over,
});

const USERS = [
  row({ id: '1', email: 'alice@example.com', full_name: 'Alice', document_count: 5, storage_bytes: 12.5 * MB, last_activity: new Date().toISOString() }),
  row({ id: '2', email: 'bob@example.com', status: 'quarantined', quarantined: true, document_count: 2, storage_bytes: 3 * MB }),
  row({ id: '3', email: 'carol@example.com', role: 'admin', document_count: 9, storage_bytes: 40 * MB }),
];

function renderAt(path: string) {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </ToastProvider>,
  );
}

const emails = () => screen.getAllByTestId('health-row').map((r) => within(r).getByText(/@example\.com/).textContent);

beforeEach(() => {
  vi.mocked(admin.fetchUserHealth).mockResolvedValue({ users: USERS.map((u) => ({ ...u })), metricsAvailable: true });
  session.start('tok', adminUser, 3600);
});

describe('access', () => {
  it('redirects non-admins and hides the nav link', async () => {
    session.start('tok', member, 3600);
    renderAt('/admin/users');
    expect(await screen.findByRole('heading', { name: /documents/i })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /user health/i })).not.toBeInTheDocument();
    expect(admin.fetchUserHealth).not.toHaveBeenCalled();
  });

  it('shows the nav link to admins', async () => {
    renderAt('/admin/users');
    expect(await screen.findByRole('link', { name: /user health/i })).toBeInTheDocument();
  });
});

describe('user health page', () => {
  it('lists users with storage in MB and totals', async () => {
    renderAt('/admin/users');
    expect(await screen.findAllByTestId('health-row')).toHaveLength(3);
    expect(screen.getByText('12.5 MB')).toBeInTheDocument();
    expect(screen.getByTestId('stat-Users')).toHaveTextContent('3');
    expect(screen.getByTestId('stat-Quarantined')).toHaveTextContent('1');
    expect(screen.getByTestId('stat-Documents')).toHaveTextContent('16');
    expect(screen.getByTestId('stat-Storage used')).toHaveTextContent('55.5 MB');
  });

  it('highlights quarantined users and lists them first', async () => {
    renderAt('/admin/users');
    const rows = await screen.findAllByTestId('health-row');
    expect(rows[0]).toHaveClass('is-quarantined');
    expect(within(rows[0]).getByText('Quarantined')).toBeInTheDocument();
    expect(rows[1]).not.toHaveClass('is-quarantined');
  });

  it('filters by search, status and role', async () => {
    renderAt('/admin/users');
    await screen.findAllByTestId('health-row');

    await userEvent.type(screen.getByLabelText('Search users'), 'alice');
    expect(emails()).toEqual(['alice@example.com']);
    await userEvent.clear(screen.getByLabelText('Search users'));

    await userEvent.selectOptions(screen.getByLabelText('Status'), 'quarantined');
    expect(emails()).toEqual(['bob@example.com']);
    await userEvent.selectOptions(screen.getByLabelText('Status'), 'all');

    await userEvent.selectOptions(screen.getByLabelText('Role'), 'admin');
    expect(emails()).toEqual(['carol@example.com']);

    await userEvent.type(screen.getByLabelText('Search users'), 'nobody');
    expect(screen.getByRole('heading', { name: /no matching users/i })).toBeInTheDocument();
  });

  it('sorts by column and toggles direction', async () => {
    renderAt('/admin/users');
    await screen.findAllByTestId('health-row');

    const storage = screen.getByRole('button', { name: /storage/i });
    await userEvent.click(storage);
    expect(emails()).toEqual(['carol@example.com', 'alice@example.com', 'bob@example.com']);
    expect(storage.closest('th')).toHaveAttribute('aria-sort', 'descending');

    await userEvent.click(storage);
    expect(emails()).toEqual(['bob@example.com', 'alice@example.com', 'carol@example.com']);

    await userEvent.click(screen.getByRole('button', { name: /^user/i }));
    expect(emails()).toEqual(['alice@example.com', 'bob@example.com', 'carol@example.com']);
  });

  it('explains when only account data is available', async () => {
    vi.mocked(admin.fetchUserHealth).mockResolvedValue({
      users: [row({ id: '1', email: 'alice@example.com', status: null, document_count: null, storage_bytes: null })],
      metricsAvailable: false,
    });
    renderAt('/admin/users');
    expect(await screen.findByText(/showing accounts only/i)).toBeInTheDocument();
    expect(screen.getByLabelText('Status')).toBeDisabled();
    expect(screen.getByTestId('stat-Quarantined')).toHaveTextContent('–');
  });

  it('shows an error and retries on refresh', async () => {
    vi.mocked(admin.fetchUserHealth).mockRejectedValueOnce(new Error('Service unavailable'));
    renderAt('/admin/users');
    expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable');
    await userEvent.click(screen.getByRole('button', { name: /refresh/i }));
    expect(await screen.findAllByTestId('health-row')).toHaveLength(3);
  });
});
