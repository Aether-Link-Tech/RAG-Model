// Collections bar on the documents page (FR-012 / AL-148): pick, create, rename, delete.
// Collections are private folders for your own documents. Deleting one moves its
// documents to "Uncategorized"; nothing is deleted.
import { useState, type FormEvent } from 'react';
import { Check, Folder, FolderOpen, Inbox, Layers, Pencil, Plus, Trash2, X } from 'lucide-react';
import Modal from './Modal';
import Button from './Button';
import { useToast } from './Toast';
import { createCollection, deleteCollection, renameCollection } from '../services/collectionService';
import { getErrorMessage } from '../services/api';
import type { Collection } from '../types';

/** 'all' = every document, 'none' = my uncategorized documents, otherwise a collection id. */
export type CollectionFilter = 'all' | 'none' | string;

interface Props {
  collections: Collection[];
  active: CollectionFilter;
  uncategorizedCount: number;
  onSelect: (filter: CollectionFilter) => void;
  onChanged: () => void;
}

export default function CollectionsBar({ collections, active, uncategorizedCount, onSelect, onChanged }: Props) {
  const toast = useToast();
  const [mode, setMode] = useState<null | 'create' | 'rename'>(null);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const activeCollection = collections.find((c) => c.id === active) ?? null;

  function open(nextMode: 'create' | 'rename') {
    setMode(nextMode);
    setName(nextMode === 'rename' ? activeCollection?.name ?? '' : '');
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    setBusy(true);
    try {
      if (mode === 'create') {
        const created = await createCollection(trimmed);
        toast.success('Collection created', created.name);
        onSelect(created.id);
      } else if (activeCollection) {
        await renameCollection(activeCollection.id, trimmed);
        toast.success('Collection renamed', trimmed);
      }
      setMode(null);
      onChanged();
    } catch (err) {
      toast.error('Could not save collection', getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!activeCollection) return;
    setBusy(true);
    try {
      await deleteCollection(activeCollection.id);
      toast.success('Collection deleted', 'Its documents moved to Uncategorized.');
      setConfirmDelete(false);
      onSelect('all');
      onChanged();
    } catch (err) {
      toast.error('Could not delete collection', getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const chip = (id: CollectionFilter, label: string, Icon: typeof Folder, count?: number) => (
    <button
      key={id}
      type="button"
      className={`collection-chip${active === id ? ' is-active' : ''}`}
      aria-pressed={active === id}
      onClick={() => onSelect(id)}
      data-testid={`collection-${id}`}
    >
      <Icon size={15} />
      <span>{label}</span>
      {count !== undefined && <small>{count}</small>}
    </button>
  );

  return (
    <div className="collections-bar" aria-label="Collections">
      <div className="collections-bar__chips">
        {chip('all', 'All documents', Layers)}
        {chip('none', 'Uncategorized', Inbox, uncategorizedCount)}
        {collections.map((c) => chip(c.id, c.name, active === c.id ? FolderOpen : Folder, c.document_count))}

        {mode ? (
          <form className="collection-form pop" onSubmit={submit}>
            <input
              autoFocus
              aria-label={mode === 'create' ? 'New collection name' : 'Collection name'}
              placeholder="Collection name"
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === 'Escape' && setMode(null)}
            />
            <button type="submit" className="icon-button" aria-label="Save collection" disabled={busy || !name.trim()}>
              <Check size={16} />
            </button>
            <button type="button" className="icon-button" aria-label="Cancel" onClick={() => setMode(null)}>
              <X size={16} />
            </button>
          </form>
        ) : (
          <button type="button" className="collection-chip collection-chip--new" onClick={() => open('create')}>
            <Plus size={15} /> New collection
          </button>
        )}
      </div>

      {activeCollection && !mode && (
        <div className="collections-bar__actions fade-up">
          <button type="button" className="icon-button" aria-label="Rename collection" onClick={() => open('rename')}>
            <Pencil size={15} />
          </button>
          <button
            type="button"
            className="icon-button icon-button--danger"
            aria-label="Delete collection"
            onClick={() => setConfirmDelete(true)}
          >
            <Trash2 size={15} />
          </button>
        </div>
      )}

      {activeCollection && (
        <Modal
          open={confirmDelete}
          title={`Delete “${activeCollection.name}”?`}
          tone="danger"
          icon={<Trash2 size={20} />}
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
                Cancel
              </Button>
              <Button variant="danger" loading={busy} loadingText="Deleting" onClick={remove}>
                Delete collection
              </Button>
            </>
          }
        >
          <p>
            The {activeCollection.document_count} document{activeCollection.document_count === 1 ? '' : 's'} in it move to
            Uncategorized. No documents are deleted.
          </p>
        </Modal>
      )}
    </div>
  );
}
