// FR-012 user-specific collections: /api/v1/collections + PUT /documents/{id} (collection_id)
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
  // The API also returns a virtual "Uncategorized" entry (id null); the page counts it itself.
  const { data } = await api.get<(Omit<Collection, 'id'> & { id: string | null })[]>('/collections');
  return data.filter((c): c is Collection => c.id !== null);
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
  const { data } = await api.put<Collection>(`/collections/${id}`, { name });
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
  // PUT /documents/{id} with only collection_id moves it; "uncategorized" clears it.
  const form = new FormData();
  form.append('collection_id', collectionId ?? 'uncategorized');
  const { data } = await api.put<Document>(`/documents/${documentId}`, form);
  return data;
}
