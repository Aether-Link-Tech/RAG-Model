import { FileText } from 'lucide-react';
import { fileKind } from '../utils/format';

// Coloured file tile: red PDF, blue DOCX, grey TXT
export default function FileIcon({ filename, size = 'md' }: { filename: string; size?: 'md' | 'lg' }) {
  const kind = fileKind(filename);
  return (
    <span className={`file-icon file-icon--${kind} file-icon--${size}`} aria-hidden="true">
      <FileText size={size === 'lg' ? 26 : 19} />
      <small>{kind === 'file' ? '' : kind}</small>
    </span>
  );
}
