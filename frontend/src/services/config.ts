export const API_URL: string = import.meta.env.VITE_API_URL || '/api/v1';

// Demo mode is on unless VITE_USE_MOCK=false is set in frontend/.env
export const USE_MOCK: boolean = (import.meta.env.VITE_USE_MOCK ?? 'true') === 'true';

// Must match UserCreate in backend/app/schemas/user.py
export const EMAIL_MIN = 3;
export const EMAIL_MAX = 255;
export const PASSWORD_MIN = 8;
export const PASSWORD_MAX = 128;
export const NAME_MAX = 100; // frontend only
