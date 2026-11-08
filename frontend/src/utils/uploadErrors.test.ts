import { describe, expect, it } from 'vitest';
import { describeUploadError } from './uploadErrors';

const httpError = (status: number, detail: string) =>
  Object.assign(new Error(detail), { isAxiosError: true, response: { status, data: { detail } } });

describe('describeUploadError (FR-068)', () => {
  it('maps ClamAV 422 to a malware rejection', () => {
    const r = describeUploadError(httpError(422, 'Malware was detected. The file was quarantined and the upload was blocked.'));
    expect(r).toMatchObject({ kind: 'malware', security: true, retryable: false });
    expect(r.reason).toMatch(/virus detected/i);
  });

  it('maps MIME mismatch to a disguised-file rejection', () => {
    expect(describeUploadError(httpError(400, 'File content does not match the .pdf extension.')).kind).toBe('disguised');
  });

  it('maps hidden extensions to a security rejection', () => {
    const r = describeUploadError(httpError(400, 'Suspicious double extension. Remove the dangerous extension'));
    expect(r).toMatchObject({ kind: 'extension', security: true });
  });

  it('maps scanner outage (503) to a retryable rejection', () => {
    expect(describeUploadError(httpError(503, 'Malware scanning is temporarily unavailable.'))).toMatchObject({
      kind: 'scanner',
      retryable: true,
    });
  });

  it('keeps the server message for duplicates (409)', () => {
    const r = describeUploadError(httpError(409, 'A private document with this content hash already exists in your workspace.'));
    expect(r.kind).toBe('duplicate');
    expect(r.reason).toContain('already exists');
  });

  it('reports network failures as retryable', () => {
    const r = describeUploadError(Object.assign(new Error('Network Error'), { isAxiosError: true }));
    expect(r).toMatchObject({ kind: 'network', retryable: true });
  });
});
