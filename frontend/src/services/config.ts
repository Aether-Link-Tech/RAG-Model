export const API_URL: string = import.meta.env.VITE_API_URL || '/api/v1';

// Demo data is opt-in: set VITE_USE_MOCK=true in frontend/.env to work without a backend.
export const USE_MOCK: boolean = (import.meta.env.VITE_USE_MOCK ?? 'false') === 'true';

// Must match UserCreate in backend/app/schemas/user.py
export const EMAIL_MIN = 3;
export const EMAIL_MAX = 255;
export const PASSWORD_MIN = 8;
export const PASSWORD_MAX = 128;
export const NAME_MAX = 100; // frontend only until FR-001 stores the name

// Must match backend upload limits (MAX_UPLOAD_BYTES, ALLOWED_MIME_TYPES)
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
export const ALLOWED_EXTENSIONS = ['.pdf', '.docx', '.txt'];

// Google reCAPTCHA v2 site key (FR-001 / FR-002). Without it (local dev), the robot check
// is a simple confirmation and the backend skips verification when it has no secret key.
export const RECAPTCHA_SITE_KEY: string = import.meta.env.VITE_RECAPTCHA_SITE_KEY ?? '';
