// FR-068 Malware rejection UX: explains why a file was refused and offers a retry.
import { RotateCcw, ShieldAlert, TriangleAlert } from 'lucide-react';
import Modal from './Modal';
import Button from './Button';
import type { UploadRejection } from '../utils/uploadErrors';

interface Props {
  rejection: (UploadRejection & { filename: string }) | null;
  onClose: () => void;
  onRetry: () => void;
  onChooseAnother: () => void;
}

export default function RejectionDialog({ rejection, onClose, onRetry, onChooseAnother }: Props) {
  if (!rejection) return null;
  const Icon = rejection.security ? ShieldAlert : TriangleAlert;

  return (
    <Modal
      open
      tone="danger"
      title={rejection.title}
      icon={<Icon size={22} />}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onChooseAnother}>
            Choose another file
          </Button>
          {rejection.retryable && (
            <Button onClick={onRetry}>
              <RotateCcw size={16} /> Retry upload
            </Button>
          )}
        </>
      }
    >
      <p className="rejection__file">{rejection.filename}</p>
      <div className="rejection__reason" data-testid="rejection-reason">
        <strong>Reason:</strong> {rejection.reason}
      </div>
      {rejection.security && (
        <p className="rejection__note">
          Files are checked for viruses, disguised file types and hidden extensions before they are stored.
          Nothing from this file was saved.
        </p>
      )}
    </Modal>
  );
}
