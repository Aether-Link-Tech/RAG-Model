// Side panel for one document: details (FR-009), download (owner only), rename/replace (FR-010),
// delete with confirmation (FR-011), publish a private document to everyone (FR-082),
// and for admins the landing-page showcase switch.
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Download, FilePen, Globe, MessageSquareText, RefreshCcw, Trash2, TriangleAlert, X } from 'lucide-react';
import Button from './Button';
import FileIcon from './FileIcon';
import Modal from './Modal';
import RejectionDialog from './RejectionDialog';
import { StatusBadge, VisibilityBadge } from './Badges';
import { useToast } from './Toast';
import { precheck } from './UploadPanel';
import {
  deleteDocument,
  downloadDocument,
  publishDocument,
  renameDocument,
  replaceDocument,
} from '../services/documentService';
import { moveDocument } from '../services/collectionService';
import { setShowcase } from '../services/adminService';
import { useSession } from '../hooks/useSession';
import { getErrorMessage, hasStatus } from '../services/api';
import { describeUploadError, type UploadRejection } from '../utils/uploadErrors';
import { extensionOf, formatBytes, formatDate } from '../utils/format';
import type { Collection, Document } from '../types';

interface Props {
  doc: Document | null;
  canDelete: boolean;
  collections?: Collection[];
  onClose: () => void;
  onChanged: (doc: Document) => void;
  onMoved?: (doc: Document) => void;
  onDeleted: (id: string) => void;
  /** Publishing replaces the private document with a new public one (new id). */
  onPublished?: (oldId: string, doc: Document) => void;
}

export default function DocumentDrawer({
  doc,
  canDelete,
  collections = [],
  onClose,
  onChanged,
  onMoved,
  onDeleted,
  onPublished,
}: Props) {
  const toast = useToast();
  const replaceInput = useRef<HTMLInputElement>(null);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState<null | 'download' | 'rename' | 'replace' | 'delete' | 'move' | 'publish' | 'showcase'>(null);
  const [confirmPublish, setConfirmPublish] = useState(false);
  const { user } = useSession();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [rejection, setRejection] = useState<(UploadRejection & { filename: string }) | null>(null);

  useEffect(() => {
    setRenaming(false);
    setConfirmDelete(false);
    setConfirmPublish(false);
    setName(doc?.filename ?? '');
  }, [doc?.id]);

  useEffect(() => {
    if (!doc) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && !confirmDelete && !confirmPublish && onClose();
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [doc, confirmDelete, confirmPublish, onClose]);

  if (!doc) return null;
  const current = doc;
  const owner = current.is_owner;
  const canPublish = owner && current.visibility === 'private';
  const publishReady = current.status === 'indexed';
  const isAdmin = user?.role === 'admin';

  async function move(collectionId: string | null) {
    if (!doc) return;
    setBusy('move');
    try {
      const updated = await moveDocument(doc.id, collectionId);
      const target = collections.find((c) => c.id === collectionId)?.name ?? 'Uncategorized';
      toast.success('Moved', `${updated.filename} → ${target}`);
      (onMoved ?? onChanged)(updated);
    } catch (err) {
      toast.error('Could not move document', getErrorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function download() {
    setBusy('download');
    try {
      await downloadDocument(current);
    } catch (error) {
      toast.error('Download failed', hasStatus(error, 404) ? 'The file is no longer available.' : getErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  async function toggleShowcase() {
    setBusy('showcase');
    try {
      const result = await setShowcase(current.id, !current.showcase);
      onChanged({ ...current, showcase: result.showcase });
      toast.success(
        result.showcase ? 'Shown on the landing page' : 'Removed from the landing page',
        result.showcase ? 'Guests can now ask questions about this document.' : current.filename,
      );
    } catch (error) {
      toast.error('Could not change landing page access', getErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  async function saveName() {
    const trimmed = name.trim();
    if (!trimmed || trimmed === current.filename) {
      setRenaming(false);
      return;
    }
    if (extensionOf(trimmed) !== extensionOf(current.filename)) {
      toast.error('Keep the file type', `The name must still end with ${extensionOf(current.filename)}.`);
      return;
    }
    setBusy('rename');
    try {
      const updated = await renameDocument(current.id, trimmed);
      onChanged(updated);
      setRenaming(false);
      toast.success('Renamed', updated.filename);
    } catch (error) {
      toast.error('Rename failed', getErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  async function replace(file: File | undefined) {
    if (!file) return;
    const problem = precheck(file);
    if (problem) {
      setRejection({ ...problem, filename: file.name });
      return;
    }
    setBusy('replace');
    try {
      const updated = await replaceDocument(current.id, file);
      onChanged(updated);
      toast.success('File replaced', 'Re-indexing in the background.');
    } catch (error) {
      setRejection({ ...describeUploadError(error), filename: file.name });
    } finally {
      setBusy(null);
    }
  }

  async function publish() {
    setBusy('publish');
    try {
      const published = await publishDocument(current.id);
      setConfirmPublish(false);
      (onPublished ?? ((_: string, d: Document) => onChanged(d)))(current.id, published);
      if (published.warning) toast.info('Published with a warning', published.warning);
      else toast.success('Published', `${published.filename} is now shared with everyone.`);
    } catch (error) {
      toast.error('Publish failed', getErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  async function remove() {
    setBusy('delete');
    try {
      await deleteDocument(current.id);
      setConfirmDelete(false);
      onDeleted(current.id);
      toast.success('Deleted', `${current.filename} and its search index were removed.`);
    } catch (error) {
      toast.error('Delete failed', getErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  return createPortal(
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label={`Document ${current.filename}`}>
        <div className="drawer__head">
          <FileIcon filename={current.filename} size="lg" />
          <div className="drawer__title">
            {renaming ? (
              <form
                className="rename-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void saveName();
                }}
              >
                <input
                  autoFocus
                  aria-label="New file name"
                  value={name}
                  maxLength={255}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => e.key === 'Escape' && setRenaming(false)}
                />
                <Button type="submit" loading={busy === 'rename'}>
                  Save
                </Button>
              </form>
            ) : (
              <h3 title={current.filename}>{current.filename}</h3>
            )}
            <div className="drawer__badges">
              <StatusBadge status={current.status} />
              <VisibilityBadge visibility={current.visibility} isOwner={owner} />
            </div>
          </div>
          <button type="button" className="icon-button" aria-label="Close details" onClick={onClose}>
            <X size={19} />
          </button>
        </div>

        {current.status === 'failed' && current.error_message && (
          <div className="drawer__error">
            <TriangleAlert size={17} />
            <span>
              <strong>Processing failed:</strong> {current.error_message}
            </span>
          </div>
        )}

        <dl className="drawer__facts">
          <div>
            <dt>Type</dt>
            <dd>{extensionOf(current.filename).slice(1).toUpperCase() || current.mime_type}</dd>
          </div>
          <div>
            <dt>Size</dt>
            <dd>{formatBytes(current.file_size)}</dd>
          </div>
          <div>
            <dt>Uploaded</dt>
            <dd>{formatDate(current.created_at)}</dd>
          </div>
          {current.updated_at && (
            <div>
              <dt>Updated</dt>
              <dd>{formatDate(current.updated_at)}</dd>
            </div>
          )}
          <div>
            <dt>Owner</dt>
            <dd>{owner ? 'You' : 'Another user (shared publicly)'}</dd>
          </div>
          {owner && (
            <div className="drawer__collection">
              <dt>Collection</dt>
              <dd>
                <select
                  aria-label="Move to collection"
                  value={current.collection_id ?? ''}
                  disabled={busy !== null}
                  onChange={(e) => void move(e.target.value || null)}
                >
                  <option value="">Uncategorized</option>
                  {collections.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </dd>
            </div>
          )}
          <div>
            <dt>SHA-256</dt>
            <dd className="mono" title={current.file_hash}>
              {current.file_hash.slice(0, 16)}…
            </dd>
          </div>
        </dl>

        <div className="drawer__actions">
          {owner && (
            <Button wide onClick={download} loading={busy === 'download'} loadingText="Preparing download">
              <Download size={17} /> Download
            </Button>
          )}
          {isAdmin && current.visibility === 'public' && current.status === 'indexed' && (
            <Button
              variant="ghost"
              wide
              onClick={toggleShowcase}
              loading={busy === 'showcase'}
              loadingText="Saving"
              disabled={busy !== null && busy !== 'showcase'}
            >
              <Globe size={16} /> {current.showcase ? 'Remove from landing page' : 'Show on landing page'}
            </Button>
          )}
          {owner && (
            <div className="drawer__row">
              <Button variant="ghost" onClick={() => setRenaming(true)} disabled={busy !== null || renaming}>
                <FilePen size={16} /> Rename
              </Button>
              <Button
                variant="ghost"
                onClick={() => replaceInput.current?.click()}
                loading={busy === 'replace'}
                loadingText="Replacing"
              >
                <RefreshCcw size={16} /> Replace file
              </Button>
            </div>
          )}
          {canPublish && (
            <Button
              variant="ghost"
              wide
              onClick={() => setConfirmPublish(true)}
              disabled={busy !== null || !publishReady}
              title={publishReady ? 'Share this document with every user' : 'Available once the document is indexed'}
            >
              <Globe size={16} /> Publish to everyone
            </Button>
          )}
          {canDelete && (
            <Button variant="danger" wide onClick={() => setConfirmDelete(true)} disabled={busy !== null}>
              <Trash2 size={16} /> Delete document
            </Button>
          )}
          {!owner && (
            <p className="muted small drawer__note">
              <MessageSquareText size={15} /> Shared publicly by another user. You can ask questions about it and see
              the cited passages; only the owner can download the file.
            </p>
          )}
        </div>

        <input
          ref={replaceInput}
          type="file"
          hidden
          accept=".pdf,.docx,.txt"
          data-testid="replace-input"
          onChange={(e) => {
            void replace(e.target.files?.[0]);
            e.target.value = '';
          }}
        />
      </aside>

      <Modal
        open={confirmDelete}
        tone="danger"
        title={`Delete ${current.filename}?`}
        icon={<Trash2 size={20} />}
        onClose={() => busy !== 'delete' && setConfirmDelete(false)}
        closable={busy !== 'delete'}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)} disabled={busy === 'delete'}>
              Cancel
            </Button>
            <Button variant="danger" onClick={remove} loading={busy === 'delete'} loadingText="Deleting">
              Delete permanently
            </Button>
          </>
        }
      >
        <p>This cannot be undone. The file, its search index and all related data will be removed.</p>
      </Modal>

      <Modal
        open={confirmPublish}
        title={`Publish ${current.filename}?`}
        icon={<Globe size={20} />}
        onClose={() => busy !== 'publish' && setConfirmPublish(false)}
        closable={busy !== 'publish'}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmPublish(false)} disabled={busy === 'publish'}>
              Cancel
            </Button>
            <Button onClick={publish} loading={busy === 'publish'} loadingText="Publishing">
              Publish
            </Button>
          </>
        }
      >
        <p>
          Every signed-in user will be able to ask questions about this document and see the cited passages; only you can
          download the file. Your name is not shown. The private copy is replaced by the public one.
        </p>
      </Modal>

      <RejectionDialog
        rejection={rejection}
        onClose={() => setRejection(null)}
        onChooseAnother={() => {
          setRejection(null);
          replaceInput.current?.click();
        }}
        onRetry={() => setRejection(null)}
      />
    </>,
    document.body,
  );
}
