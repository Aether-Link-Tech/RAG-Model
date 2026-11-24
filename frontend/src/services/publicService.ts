// Landing page (no sign-in): guest questions over the showcase documents an admin selected.
// POST /api/v1/public/ask, GET /api/v1/public/documents. Ask-only: no files, no downloads.
import api from './api';

export interface PublicSource {
  marker: number;
  document_name: string;
  page: number | null;
  section: string | null;
  source_type: 'text' | 'ocr';
  text: string; // short excerpt around the highlighted sentence
  highlight: string | null;
  score: number;
}

export interface PublicAnswer {
  answer: string;
  sources: PublicSource[];
  insufficient: boolean;
  uncertain: boolean;
  uncertain_reason: string | null;
}

export const PUBLIC_QUESTION_MAX = 500; // PUBLIC_QUESTION_MAX_CHARS in backend/app/core/config.py

export async function askPublic(question: string): Promise<PublicAnswer> {
  const { data } = await api.post<PublicAnswer>('/public/ask', { question });
  return data;
}

export async function listShowcase(): Promise<string[]> {
  const { data } = await api.get<{ name: string }[]>('/public/documents');
  return data.map((d) => d.name);
}
