# Frontend structure guide

React 19 + TypeScript + Vite 8 + Tailwind v4 + React Router 7 + Axios + lucide-react icons.

## Run it

```bash
cd frontend
npm install
npm run dev          # http://localhost:3000  ->  opens /signup
```

By default the UI uses **demo data** (no backend needed).
To use the real backend: copy `.env.example` to `.env`, set `VITE_USE_MOCK=false`, restart `npm run dev`.
Vite forwards every `/api/...` call to `http://localhost:8000` (see `vite.config.ts`).

## Folder responsibilities

- `src/pages/`: full route-level screens (one file = one screen).
- `src/components/`: reusable UI pieces (buttons, inputs, captcha box...).
- `src/layouts/`: shared page frames (`AuthLayout` = dark background + header for auth pages).
- `src/services/`: everything that talks to the backend (`api.ts`, `authService.ts`...).
- `src/hooks/`: reusable React hooks.
- `src/types/`: shared TypeScript types (mirror `backend/app/schemas/`).
- `src/styles/`: CSS split by area (`animations.css`, `components.css`, `auth.css`).
- `src/assets/images/`: images imported by components/pages (logo, background).
- `public/images/`: static files referenced by URL (`/images/favicon.png`).

## Done tasks

### AL-119 — Build register form UI (`/signup`)

| File | What it does |
| --- | --- |
| `src/pages/signup.tsx` | The register form: name, email, password, confirm, captcha. `validate()` checks the rules, then calls the service. Shows "Check your email" on success and a clear message for a duplicate email (409). |
| `src/layouts/AuthLayout.tsx` | Background, header, welcome text and the glass card around the form. |
| `src/components/InputField.tsx` | Input with label, icon and show/hide password eye. |
| `src/components/RobotCheck.tsx` | "I'm not a robot" box: click -> loading -> verified (visual only for now). |
| `src/components/Button.tsx` | Button with a loading spinner. |
| `src/components/Alert.tsx` | Red error message box. |
| `src/components/Logo.tsx` | Aether Link logo + name. |
| `src/assets/images/network-background.svg` | Animated background (lines pulse, dots twinkle, spheres float). The animation is inside the SVG file. |
| `src/services/authService.ts` | `signup(email, password)` -> `POST /api/v1/users`. |
| `src/services/api.ts` | Shared axios instance + `getErrorMessage()` + `hasStatus()`. |
| `src/services/config.ts` | API URL, demo switch, and the signup limits (same as backend `UserCreate`). |
| `src/services/mockData.ts` | Demo data. `taken@example.com` acts as an existing email. |
| `src/types/index.ts` | `User` type (same as backend `UserResponse`). |
| `src/App.tsx` | Routes: `/signup`; every other URL redirects to `/signup` for now. |

Validation rules (from `backend/app/schemas/user.py`): email 3–255 characters and valid format,
password 8–128 characters, passwords must match, name required (max 100, frontend only), captcha ticked.

Backend notes: the backend does not store the full name, has no CAPTCHA check and does not send
confirmation emails yet. The UI shows "Check your email" as the task requires.
