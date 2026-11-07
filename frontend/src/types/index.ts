// Mirrors UserResponse in backend/app/schemas/user.py
export interface User {
  id: string;
  email: string;
  role: string;
  created_at: string;
  is_deleted: boolean;
}
