import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from '../App';
import { ToastProvider } from '../components/Toast';
import { session } from '../services/session';
import type { Document, User } from '../types';

vi.mock('../services/documentService', () => ({
  listDocuments: vi.fn(),
  uploadDocument: vi.fn(),
  deleteDocument: vi.fn(),
  downloadDocument: vi.fn(),
  renameDocument: vi.fn(),
  replaceDocument: vi.fn(),
  retryDocument: vi.fn(),
  publishDocument: vi.fn(),
}));
vi.mock('../services/authService', () => ({
  login: vi.fn(),
  signup: vi.fn(),
  logout: vi.fn(async () => session.clear()),
}));

import * as docs from '../services/documentService';

const me: User = { id: 'me', email: 'owner@example.com', role: 'user', created_at: '', is_deleted: false };
const doc = (over: Partial<Document>): Document => ({
  id: 'x', filename: 'file.pdf', mime_type: 'application/pdf', file_size: 2048, file_hash: 'f'.repeat(64),
  status: 'indexed', visibility: 'private', is_owner: true, user_id: 'me', error_message: null,
  created_at: new Date().toISOString(), updated_at: null, ...over,
});

const DOCS = [
  doc({ id: 'a', filename: 'Annual report.pdf' }),
  doc({ id: 'b', filename: 'Handbook.docx', visibility: 'public', is_owner: false, user_id: null }),
  doc({ id: 'c', filename: 'Notes.txt', status: 'failed', error_message: 'No extractable text found in document' }),
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

beforeEach(() => {
  vi.mocked(docs.listDocuments).mockResolvedValue([...DOCS]);
  session.start('tok', me, 3600);
});

describe('routing & auth', () => {
  it('sends signed-out users to the login page', async () => {
    session.clear();
    renderAt('/documents');
    expect(await screen.findByRole('heading', { name: /welcome back/i })).toBeInTheDocument();
  });

  it('logs out from the app bar', async () => {
    renderAt('/documents');
    await userEvent.click(await screen.findByRole('button', { name: /log out/i }));
    expect(await screen.findByRole('heading', { name: /welcome back/i })).toBeInTheDocument();
    expect(session.isAuthenticated()).toBe(false);
  });
});

describe('documents page', () => {
  it('lists documents with status and visibility', async () => {
    renderAt('/documents');
    const rows = await screen.findAllByTestId('doc-row');
    expect(rows).toHaveLength(3);
    expect(within(rows[1]).getByText('Shared')).toBeInTheDocument();
    expect(within(rows[2]).getByText('Failed')).toBeInTheDocument();
    expect(screen.getByTestId('stat-Documents')).toHaveTextContent('3');
  });

  it('filters and searches', async () => {
    renderAt('/documents');
    await screen.findAllByTestId('doc-row');
    await userEvent.click(screen.getByRole('tab', { name: /needs attention/i }));
    expect(screen.getAllByTestId('doc-row')).toHaveLength(1);
    await userEvent.click(screen.getByRole('tab', { name: /^all$/i }));
    await userEvent.type(screen.getByLabelText(/search documents/i), 'hand');
    expect(screen.getAllByTestId('doc-row')).toHaveLength(1);
  });

  it('shows details and the processing error in the drawer', async () => {
    renderAt('/documents');
    await userEvent.click((await screen.findAllByTestId('doc-row'))[2]);
    const drawer = await screen.findByRole('dialog', { name: /notes\.txt/i });
    expect(within(drawer).getByText(/no extractable text/i)).toBeInTheDocument();
  });

  it('retries an owned failed document and refreshes its status', async () => {
    const retried = doc({ id: 'c', filename: 'Notes.txt', status: 'processing', error_message: null });
    vi.mocked(docs.listDocuments)
      .mockResolvedValueOnce([...DOCS])
      .mockResolvedValueOnce([DOCS[0], DOCS[1], retried]);
    vi.mocked(docs.retryDocument).mockResolvedValue(retried);
    renderAt('/documents');
    await screen.findAllByTestId('doc-row');

    await userEvent.click(screen.getByRole('button', { name: /retry processing notes\.txt/i }));

    await waitFor(() => expect(docs.retryDocument).toHaveBeenCalledWith('c'));
    await waitFor(() => {
      const rows = screen.getAllByTestId('doc-row');
      expect(within(rows.find((row) => row.textContent?.includes('Notes.txt'))!).getByText('Processing')).toBeInTheDocument();
    });
    expect(docs.listDocuments).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('button', { name: /retry processing notes\.txt/i })).not.toBeInTheDocument();
  });

  it('does not offer retry for a failed public document owned by someone else', async () => {
    vi.mocked(docs.listDocuments).mockResolvedValue([
      doc({ id: 'public-failed', status: 'failed', visibility: 'public', is_owner: false, user_id: null }),
    ]);
    renderAt('/documents');
    await screen.findByTestId('doc-row');

    expect(screen.queryByRole('button', { name: /retry processing/i })).not.toBeInTheDocument();
  });

  it('keeps failed document retry available when queueing fails', async () => {
    vi.mocked(docs.retryDocument).mockRejectedValue(
      Object.assign(new Error('x'), {
        isAxiosError: true,
        response: { status: 503, data: { detail: 'Document retry could not be queued. Please try again.' } },
      }),
    );
    renderAt('/documents');
    await screen.findAllByTestId('doc-row');

    await userEvent.click(screen.getByRole('button', { name: /retry processing notes\.txt/i }));

    expect(await screen.findByText(/could not retry document/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry processing notes\.txt/i })).toBeInTheDocument();
  });

  it('hides owner actions on documents shared by others', async () => {
    renderAt('/documents');
    await userEvent.click((await screen.findAllByTestId('doc-row'))[1]);
    const drawer = await screen.findByRole('dialog', { name: /handbook/i });
    expect(within(drawer).queryByRole('button', { name: /delete document/i })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole('button', { name: /rename/i })).not.toBeInTheDocument();
    // Ask-only access: only the owner can download the file.
    expect(within(drawer).queryByRole('button', { name: /download/i })).not.toBeInTheDocument();
    expect(within(drawer).getByText(/ask questions about it/i)).toBeInTheDocument();
  });

  it('lets the owner download their own document', async () => {
    renderAt('/documents');
    await userEvent.click((await screen.findAllByTestId('doc-row'))[0]);
    const drawer = await screen.findByRole('dialog', { name: /annual report/i });
    expect(within(drawer).getByRole('button', { name: /download/i })).toBeInTheDocument();
    expect(within(drawer).queryByRole('button', { name: /landing page/i })).not.toBeInTheDocument();
  });

  it('deletes only after confirmation (FR-011)', async () => {
    vi.mocked(docs.deleteDocument).mockResolvedValue();
    renderAt('/documents');
    await userEvent.click((await screen.findAllByTestId('doc-row'))[0]);
    await userEvent.click(await screen.findByRole('button', { name: /delete document/i }));
    const confirm = await screen.findByRole('dialog', { name: /delete annual report\.pdf\?/i });
    expect(docs.deleteDocument).not.toHaveBeenCalled();
    await userEvent.click(within(confirm).getByRole('button', { name: /delete permanently/i }));
    await waitFor(() => expect(screen.getAllByTestId('doc-row')).toHaveLength(2));
    expect(docs.deleteDocument).toHaveBeenCalledWith('a');
  });

  it('explains a malware rejection with the reason (FR-068)', async () => {
    vi.mocked(docs.uploadDocument).mockRejectedValue(
      Object.assign(new Error('x'), {
        isAxiosError: true,
        response: { status: 422, data: { detail: 'Malware was detected. The file was quarantined and the upload was blocked.' } },
      }),
    );
    renderAt('/documents');
    await screen.findAllByTestId('doc-row');
    await userEvent.upload(screen.getByTestId('file-input'), new File(['bad'], 'invoice.pdf', { type: 'application/pdf' }));
    await userEvent.click(screen.getByRole('button', { name: /upload document/i }));
    const dialog = await screen.findByRole('dialog', { name: /security check failed/i });
    expect(within(dialog).getByTestId('rejection-reason')).toHaveTextContent(/virus detected/i);
  });

  it('uploads with the chosen visibility and shows the new document', async () => {
    vi.mocked(docs.uploadDocument).mockResolvedValue(doc({ id: 'n', filename: 'New.txt', visibility: 'public' }));
    renderAt('/documents');
    await screen.findAllByTestId('doc-row');
    await userEvent.upload(screen.getByTestId('file-input'), new File(['hello'], 'New.txt', { type: 'text/plain' }));
    await userEvent.click(screen.getByRole('radio', { name: /public/i }));
    await userEvent.click(screen.getByRole('button', { name: /upload document/i }));
    await waitFor(() => expect(screen.getAllByTestId('doc-row')).toHaveLength(4));
    expect(vi.mocked(docs.uploadDocument).mock.calls[0][1]).toBe('public');
  });
  it('publishes a private document only after confirmation (FR-082)', async () => {
    vi.mocked(docs.publishDocument).mockResolvedValue(doc({ id: 'a2', filename: 'Annual report.pdf', visibility: 'public' }));
    renderAt('/documents');
    await userEvent.click((await screen.findAllByTestId('doc-row'))[0]);
    await userEvent.click(await screen.findByRole('button', { name: /publish to everyone/i }));
    const confirm = await screen.findByRole('dialog', { name: /publish annual report\.pdf\?/i });
    expect(docs.publishDocument).not.toHaveBeenCalled();
    await userEvent.click(within(confirm).getByRole('button', { name: /^publish$/i }));
    await waitFor(() => expect(docs.publishDocument).toHaveBeenCalledWith('a'));
    // the private row is replaced by the new public one
    await waitFor(() => expect(screen.getAllByTestId('doc-row')).toHaveLength(3));
    expect(screen.getByTestId('stat-Public')).toHaveTextContent('2');
  });

  it('does not offer publishing for documents that are not indexed', async () => {
    renderAt('/documents');
    await userEvent.click((await screen.findAllByTestId('doc-row'))[2]);
    const drawer = await screen.findByRole('dialog', { name: /notes\.txt/i });
    expect(within(drawer).getByRole('button', { name: /publish to everyone/i })).toBeDisabled();
  });
});
