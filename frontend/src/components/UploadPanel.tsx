// FR-008 upload UI: drag-drop or browse, private/public toggle (FR-077), progress bar.
import { useRef, useState, type DragEvent } from 'react';
import { CloudUpload, FileUp, Globe, Info, Lock, X } from 'lucide-react';
import Button from './Button';
import FileIcon from './FileIcon';
import RejectionDialog from './RejectionDialog';
import { useToast } from './Toast';
import { uploadDocument } from '../services/documentService';
import { ALLOWED_EXTENSIONS, MAX_UPLOAD_BYTES } from '../services/config';
import { describeUploadError, type UploadRejection } from '../utils/uploadErrors';
import { extensionOf, formatBytes } from '../utils/format';
import type { Document, DocumentVisibility } from '../types';

interface Props {
  onUploaded: (doc: Document) => void;
}

// Checks we can do before sending anything (the server re-checks everything)
export function precheck(file: File): UploadRejection | null {
  if (!ALLOWED_EXTENSIONS.includes(extensionOf(file.name))) {
    return { kind: 'type', title: 'Unsupported file type', reason: 'Only PDF, DOCX and TXT files are allowed.', security: false, retryable: false };
  }
  if (file.size === 0) {
    return { kind: 'empty', title: 'Empty file', reason: 'The file is empty (0 bytes).', security: false, retryable: false };
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return { kind: 'size', title: 'File too large', reason: `Maximum size is ${formatBytes(MAX_UPLOAD_BYTES)}.`, security: false, retryable: false };
  }
  return null;
}

export default function UploadPanel({ onUploaded }: Props) {
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [visibility, setVisibility] = useState<DocumentVisibility>('private');
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [rejection, setRejection] = useState<(UploadRejection & { filename: string }) | null>(null);

  function pick(candidate: File | undefined) {
    if (!candidate) return;
    const problem = precheck(candidate);
    if (problem) {
      setRejection({ ...problem, filename: candidate.name });
      return;
    }
    setFile(candidate);
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    pick(event.dataTransfer.files?.[0]);
  }

  async function upload() {
    if (!file) return;
    setProgress(0);
    try {
      const doc = await uploadDocument(file, visibility, setProgress);
      toast.success('Uploaded', `${doc.filename} is ${doc.status === 'indexed' ? 'ready' : 'being processed'}.`);
      onUploaded(doc);
      setFile(null);
      setVisibility('private');
    } catch (error) {
      setRejection({ ...describeUploadError(error), filename: file.name });
    } finally {
      setProgress(null);
    }
  }

  function chooseAnother() {
    setRejection(null);
    setFile(null);
    inputRef.current?.click();
  }

  const busy = progress !== null;

  return (
    <section className="panel upload-panel" aria-label="Upload a document">
      <div className="panel__head">
        <h2>
          <CloudUpload size={19} /> Upload
        </h2>
        <span className="muted">PDF, DOCX, TXT · up to {formatBytes(MAX_UPLOAD_BYTES)}</span>
      </div>

      {!file ? (
        <div
          className={`dropzone${dragging ? ' dropzone--active' : ''}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          onClick={() => inputRef.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && inputRef.current?.click()}
          data-testid="dropzone"
        >
          <span className="dropzone__icon">
            <FileUp size={26} />
          </span>
          <strong>Drag & drop a file here</strong>
          <span className="muted">
            or <u>browse your computer</u>
          </span>
        </div>
      ) : (
        <div className="selected-file">
          <FileIcon filename={file.name} />
          <div className="selected-file__meta">
            <strong title={file.name}>{file.name}</strong>
            <span className="muted">
              {formatBytes(file.size)} · {extensionOf(file.name).slice(1).toUpperCase()}
            </span>
          </div>
          {!busy && (
            <button type="button" className="icon-button" aria-label="Remove file" onClick={() => setFile(null)}>
              <X size={17} />
            </button>
          )}
        </div>
      )}

      <input
        ref={inputRef}
        type="file"
        accept={ALLOWED_EXTENSIONS.join(',')}
        hidden
        data-testid="file-input"
        onChange={(e) => {
          pick(e.target.files?.[0]);
          e.target.value = '';
        }}
      />

      <div className="visibility-toggle" role="radiogroup" aria-label="Visibility">
        <button
          type="button"
          role="radio"
          aria-checked={visibility === 'private'}
          className={visibility === 'private' ? 'is-active' : ''}
          onClick={() => setVisibility('private')}
          disabled={busy}
        >
          <Lock size={15} /> Private
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={visibility === 'public'}
          className={visibility === 'public' ? 'is-active' : ''}
          onClick={() => setVisibility('public')}
          disabled={busy}
        >
          <Globe size={15} /> Public
        </button>
        <span className="tooltip" tabIndex={0} aria-label="Public documents can be seen by all users">
          <Info size={15} />
          <span className="tooltip__bubble">Public documents can be seen by all users. Visibility can't be changed after upload.</span>
        </span>
      </div>

      {busy && (
        <div className="progress" aria-label="Upload progress">
          <div className="progress__bar" style={{ width: `${progress}%` }} />
          <span>{progress! < 100 ? `Uploading… ${progress}%` : 'Scanning & processing…'}</span>
        </div>
      )}

      <Button wide disabled={!file} loading={busy} loadingText="Uploading" onClick={upload}>
        <CloudUpload size={18} /> Upload document
      </Button>

      <RejectionDialog
        rejection={rejection}
        onClose={() => setRejection(null)}
        onChooseAnother={chooseAnother}
        onRetry={() => {
          setRejection(null);
          void upload();
        }}
      />
    </section>
  );
}
