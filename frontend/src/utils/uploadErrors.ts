// Turns backend upload rejections into a clear reason for the user (FR-068).
import axios from 'axios';
import { getErrorMessage, statusOf } from '../services/api';

export type RejectionKind =
  | 'malware'
  | 'disguised'
  | 'extension'
  | 'type'
  | 'size'
  | 'empty'
  | 'duplicate'
  | 'scanner'
  | 'network'
  | 'other';

export interface UploadRejection {
  kind: RejectionKind;
  title: string;
  reason: string;
  security: boolean; // rejected by a security check (FR-064/065/066)
  retryable: boolean;
}

export function describeUploadError(error: unknown): UploadRejection {
  const status = statusOf(error);
  const detail = getErrorMessage(error, '');
  const text = detail.toLowerCase();

  if (axios.isAxiosError(error) && !error.response) {
    return { kind: 'network', title: 'Upload failed', reason: 'Cannot reach the server. Check your connection and try again.', security: false, retryable: true };
  }
  if (status === 422 && text.includes('malware')) {
    return { kind: 'malware', title: 'File rejected: security check failed', reason: 'Virus detected. The file was quarantined and was not stored.', security: true, retryable: false };
  }
  if (status === 503) {
    return { kind: 'scanner', title: 'Security scan unavailable', reason: 'The malware scanner is temporarily unavailable, so the file was not accepted. Please try again in a moment.', security: true, retryable: true };
  }
  if (status === 409) {
    return { kind: 'duplicate', title: 'Already uploaded', reason: detail || 'This document already exists.', security: false, retryable: false };
  }
  if (status === 400) {
    if (text.includes('double extension') || text.includes('archive extension') || text.includes('multiple extensions')) {
      return { kind: 'extension', title: 'File rejected: security check failed', reason: 'Suspicious file name: a dangerous extension is hidden before the file type (e.g. report.exe.pdf).', security: true, retryable: false };
    }
    if (text.includes('does not match') || text.includes('signature')) {
      return { kind: 'disguised', title: 'File rejected: security check failed', reason: "MIME mismatch: the file's content does not match its extension (possible disguised file).", security: true, retryable: false };
    }
    if (text.includes('invalid file type')) {
      return { kind: 'type', title: 'Unsupported file type', reason: 'Only PDF, DOCX and TXT files are allowed.', security: false, retryable: false };
    }
    if (text.includes('size')) {
      return { kind: 'size', title: 'File too large', reason: detail, security: false, retryable: false };
    }
    if (text.includes('empty')) {
      return { kind: 'empty', title: 'Empty file', reason: 'The file is empty (0 bytes).', security: false, retryable: false };
    }
  }
  return { kind: 'other', title: 'Upload failed', reason: detail || 'Something went wrong. Please try again.', security: false, retryable: true };
}
