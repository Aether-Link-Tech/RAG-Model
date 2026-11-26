export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

// Storage is always shown in MB on the admin dashboard (FR-076), so values compare at a glance.
export function formatMegabytes(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  if (mb === 0) return '0 MB';
  if (mb < 0.1) return '< 0.1 MB';
  return `${mb.toLocaleString(undefined, { maximumFractionDigits: mb < 100 ? 1 : 0 })} MB`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

export function relativeTime(iso: string, now = Date.now()): string {
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000);
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} d ago`;
  return formatDate(iso);
}

export function fileKind(filename: string): 'pdf' | 'docx' | 'txt' | 'file' {
  const ext = filename.split('.').pop()?.toLowerCase();
  return ext === 'pdf' || ext === 'docx' || ext === 'txt' ? ext : 'file';
}

export function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf('.');
  return dot >= 0 ? filename.slice(dot).toLowerCase() : '';
}
