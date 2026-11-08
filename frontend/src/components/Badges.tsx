import { CircleAlert, CircleCheck, Clock, Globe, Lock, Users } from 'lucide-react';
import type { DocumentStatus, DocumentVisibility } from '../types';

const STATUS = {
  pending: { label: 'Queued', Icon: Clock },
  processing: { label: 'Processing', Icon: null },
  indexed: { label: 'Ready', Icon: CircleCheck },
  failed: { label: 'Failed', Icon: CircleAlert },
} as const;

export function StatusBadge({ status }: { status: DocumentStatus }) {
  const { label, Icon } = STATUS[status];
  return (
    <span className={`badge badge--${status}`} data-testid="status-badge">
      {Icon ? <Icon size={13} /> : <span className="spinner spinner--xs" />}
      {label}
    </span>
  );
}

export function VisibilityBadge({ visibility, isOwner }: { visibility: DocumentVisibility; isOwner: boolean }) {
  if (visibility === 'private') {
    return (
      <span className="badge badge--private">
        <Lock size={12} /> Private
      </span>
    );
  }
  return (
    <span className="badge badge--public">
      {isOwner ? <Globe size={12} /> : <Users size={12} />}
      {isOwner ? 'Public' : 'Shared'}
    </span>
  );
}
