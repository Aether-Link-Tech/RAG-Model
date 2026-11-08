// FR-012 user-specific collections: /api/v1/collections + PUT /documents/{id}/collection
import api from './api';
import { USE_MOCK } from './config';
import { wait } from './mockData';
import type { Collection, Document } from '../types';

let mockCollections: Collection[] = [
  { id: 'c-research', name: 'Research', document_count: 1 },
  { id: 'c-hr', name: 'HR policies', document_count: 0 },
];

export async function listCollections(): Promise<Collection[]> {
  if (USE_MOCK) {
    await wait(250);
    return [...mockCollections];
  }
  const { data } = await api.get<Collection[]>('/collections');
  return data;
}

export async function createCollection(name: string): Promise<Collection> {
  if (USE_MOCK) {
    await wait(300);
    const created = { id: `c-${Date.now()}`, name: name.trim(), document_count: 0 };
    mockCollections = [...mockCollections, created];
    return created;
  }
  const { data } = await api.post<Collection>('/collections', { name });
  return data;
}

export async function renameCollection(id: string, name: string): Promise<Collection> {
  if (USE_MOCK) {
    await wait(300);
    mockCollections = mockCollections.map((c) => (c.id === id ? { ...c, name: name.trim() } : c));
    return mockCollections.find((c) => c.id === id)!;
  }
  const { data } = await api.patch<Collection>(`/collections/${id}`, { name });
  return data;
}

export async function deleteCollection(id: string): Promise<void> {
  if (USE_MOCK) {
    await wait(300);
    mockCollections = mockCollections.filter((c) => c.id !== id);
    return;
  }
  await api.delete(`/collections/${id}`);
}

/** Move one of your documents into a collection; null = Uncategorized. */
export async function moveDocument(documentId: string, collectionId: string | null): Promise<Document> {
  const { data } = await api.put<Document>(`/documents/${documentId}/collection`, {
    collection_id: collectionId,
  });
  return data;
}
