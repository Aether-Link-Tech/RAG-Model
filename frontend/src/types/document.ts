export type DocumentStatus =
  | "pending"
  | "processing"
  | "indexed"
  | "failed";

export type DocumentVisibility = "private" | "public";

export interface Document {
  id: string;
  filename: string;
  mime_type: string;
  file_size: number;
  status: DocumentStatus;
  visibility: DocumentVisibility;
  is_owner: boolean;
  user_id: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: {
    id: string;
    email: string;
    [key: string]: unknown;
  };
}