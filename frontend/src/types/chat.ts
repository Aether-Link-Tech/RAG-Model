// Mirrors backend/app/schemas/chat.py (FR-028 Q&A contract)

export interface ChatAskRequest {
  question: string;
  conversation_id?: string | null;
  include_public?: boolean;
  include_private?: boolean;
}

export interface ChatSource {
  marker: number; // the [n] used in the answer
  document_id: string;
  document_name: string;
  page: number | null;
  section: string | null;
  source_type: 'text' | 'ocr';
  text: string;
  highlight?: string | null;
  score: number; // 0..1
}

export interface ClaimCheck {
  text: string;
  markers: number[];
  supported: boolean;
  score: number;
  chunk_id: string | null;
  reason: string | null;
}

export type ComparisonRelation = 'agree' | 'differ' | 'contradict';

export interface ComparisonPair {
  relation: ComparisonRelation;
  document_a: string;
  quote_a: string;
  document_b: string;
  quote_b: string;
  similarity: number;
}

export interface Comparison {
  relation: ComparisonRelation | 'unrelated';
  documents: string[];
  pairs: ComparisonPair[];
}

export interface ChatAnswer {
  conversation_id: string;
  message_id: string;
  answer: string;
  sources: ChatSource[];
  insufficient: boolean;
  uncertain: boolean;
  uncertain_reason: string | null;
  claims: ClaimCheck[];
  removed_claims: number;
  injection_detected: boolean;
  comparison: Comparison | null;
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface ConversationMessage {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  sources: ChatSource[];
  insufficient: boolean;
  uncertain: boolean;
  uncertain_reason: string | null;
  claims: ClaimCheck[];
  removed_claims: number;
  injection_detected: boolean;
  comparison: Comparison | null;
  created_at: string;
}

export interface ConversationDetail {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  messages: ConversationMessage[];
}
