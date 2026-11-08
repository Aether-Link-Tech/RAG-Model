// Documents API (FR-008 upload, FR-009 view/download, FR-010 update, FR-011 delete)
import api from './api';
import { USE_MOCK } from './config';
import { mockDocuments, wait } from './mockData';
import type { Document, DocumentVisibility } from '../types';

export async function listDocuments(): Promise<Document[]> {
  if (USE_MOCK) {
    await wait(500);
    return [...mockDocuments];
  }
  const { data } = await api.get<Document[]>('/documents');
  return data;
}

export async function getDocument(id: string): Promise<Document> {
  if (USE_MOCK) {
    const doc = mockDocuments.find((d) => d.id === id);
    if (!doc) throw new Error('Document not found');
    return doc;
  }
  const { data } = await api.get<Document>(`/documents/${id}`);
  return data;
}

export async function uploadDocument(
  file: File,
  visibility: DocumentVisibility,
  onProgress?: (percent: number) => void,
): Promise<Document> {
  if (USE_MOCK) {
    for (let p = 0; p <= 100; p += 20) {
      onProgress?.(p);
      await wait(120);
    }
    const doc: Document = {
      id: crypto.randomUUID(),
      filename: file.name,
      mime_type: file.type || 'text/plain',
      file_size: file.size,
      file_hash: '0'.repeat(64),
      status: 'indexed',
      visibility,
      is_owner: true,
      user_id: 'mock',
      error_message: null,
      created_at: new Date().toISOString(),
      updated_at: null,
    };
    mockDocuments.unshift(doc);
    return doc;
  }

  const form = new FormData();
  form.append('file', file);
  form.append('visibility', visibility);
  const { data } = await api.post<Document>('/documents', form, {
    onUploadProgress: (event) => {
      if (event.total) onProgress?.(Math.round((event.loaded / event.total) * 100));
    },
  });
  return data;
}

export async function renameDocument(id: string, filename: string): Promise<Document> {
  if (USE_MOCK) {
    const doc = mockDocuments.find((d) => d.id === id)!;
    doc.filename = filename;
    return { ...doc };
  }
  const form = new FormData();
  form.append('filename', filename);
  const { data } = await api.put<Document>(`/documents/${id}`, form);
  return data;
}

export async function replaceDocument(id: string, file: File, onProgress?: (percent: number) => void): Promise<Document> {
  if (USE_MOCK) {
    const doc = mockDocuments.find((d) => d.id === id)!;
    Object.assign(doc, { filename: file.name, file_size: file.size, status: 'indexed' as const });
    return { ...doc };
  }
  const form = new FormData();
  form.append('file', file);
  const { data } = await api.put<Document>(`/documents/${id}`, form, {
    onUploadProgress: (event) => {
      if (event.total) onProgress?.(Math.round((event.loaded / event.total) * 100));
    },
  });
  return data;
}

export async function deleteDocument(id: string): Promise<void> {
  if (USE_MOCK) {
    const index = mockDocuments.findIndex((d) => d.id === id);
    if (index >= 0) mockDocuments.splice(index, 1);
    return;
  }
  await api.delete(`/documents/${id}`);
}

// Streams the original file and triggers a browser download (no public URL is exposed).
export async function downloadDocument(doc: Document): Promise<void> {
  if (USE_MOCK) {
    saveBlob(new Blob([`Demo content of ${doc.filename}`], { type: 'text/plain' }), doc.filename);
    return;
  }
  const response = await api.get<Blob>(`/documents/${doc.id}/download`, { responseType: 'blob' });
  saveBlob(response.data, doc.filename);
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
