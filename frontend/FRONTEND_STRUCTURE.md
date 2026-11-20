# Frontend structure guide

React 19 + TypeScript + Vite 8 + Tailwind v4 + React Router 7 + Axios + lucide-react icons.

## Run it

### Whole stack with Docker (recommended)

```bash
# from the repository root
cp .env.example .env                                      # once
docker build -t rag-parser-sandbox:latest docker/parser   # once / after parser changes
docker compose up -d --build
ollama pull nomic-embed-text && ollama pull qwen2.5:3b     # Ollama runs on the host
```

| Open | What |
| --- | --- |
| https://localhost | The app (Caddy → nginx in the `frontend` container → `/api` to the backend). Trust Caddy's local certificate once: `docker compose exec caddy caddy trust`, or accept the browser warning. |
| http://localhost:8025 | Mailpit inbox: the verification email for every sign-up lands here. Click the link to verify. |
| http://localhost:8000/docs | API docs (loopback only). |

Sign up → open Mailpit → click the link (`https://localhost/verify-email?token=…`) → sign in.
The link address comes from `EMAIL_VERIFICATION_URL_DOCKER` (default `https://localhost/verify-email`);
`EMAIL_VERIFICATION_URL` in `.env` is only for `npm run dev`.

After changing frontend code: `docker compose up -d --build frontend`.

### Frontend only (hot reload)

```bash
cd frontend
npm install
npm run dev          # http://localhost:3000, forwards /api/... to http://localhost:8000
```

The UI talks to the real backend by default. For demo data without a backend, create `frontend/.env`
with `VITE_USE_MOCK=true` (see `.env.example`) and restart `npm run dev`.

`npm test` runs the Vitest suite, `npm run build` type-checks and builds.

## Backend connection map

Every call goes through `src/services/` (never call axios/fetch from a page). Requests carry the
Bearer token from `services/session.ts`; a 401 triggers one silent refresh, then a redirect to `/login`.

| Screen | Service | Endpoint |
| --- | --- | --- |
| Sign up `/signup` | `authService.signup` | `POST /auth/register` |
| Verify email `/verify-email?token=` | `authService.verifyEmail`, `resendVerification` | `GET /auth/verify-email`, `POST /auth/resend-verification` |
| Sign in `/login` | `authService.login` | `POST /auth/login` (captcha after 3 failures, 423 lockout, 403 unverified) |
| Session | `api.refreshSession`, `authService.logout` | `POST /auth/refresh`, `POST /auth/logout` |
| Documents `/documents` | `documentService` | `GET/POST /documents`, `GET/PUT/DELETE /documents/{id}`, `GET /documents/{id}/download`, `POST /documents/{id}/publish` |
| Collections (documents page) | `collectionService` | `GET/POST /collections`, `PUT/DELETE /collections/{id}`, `PUT /documents/{id}` (`collection_id`) |
| Ask `/chat` | `chatService.ask` (fetch, streamed) | `POST /chat/ask` with `Accept: text/event-stream` |
| Audit log `/admin/audit` (admins) | `adminService` | `GET /admin/audit-log` |

Not used by the UI yet (backend ready): `GET /users/me`, `DELETE /users/me`, `DELETE /users/me/private-collection`,
`GET /admin/users`, `PATCH /admin/users/{id}/role`, `GET /admin/roles`, `POST /retrieval/query`.

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

### Backend connection (front-end branch)

| File | What it does |
| --- | --- |
| `src/pages/chat.tsx` | Ask your documents: streamed answer, `[n]` citation chips that highlight the source, source cards (page, section, OCR, relevance), notices for insufficient / uncertain / removed statements / prompt injection, comparison view, scope toggles (private / shared), follow-ups keep `conversation_id`, stop and retry. |
| `src/services/chatService.ts` | `ask()` → `POST /chat/ask` as Server-Sent Events (`token`, `done`, `error`); same token refresh rules as `api.ts`; accepts a plain JSON answer too. |
| `src/types/chat.ts` | Mirrors `backend/app/schemas/chat.py`. |
| `src/styles/chat.css` | Chat page styles. |
| `src/components/DocumentDrawer.tsx` | "Publish to everyone" for your indexed private documents, with confirmation. The backend replaces the private document with a new public one (new id). |
| `src/services/documentService.ts` | `publishDocument()` → `POST /documents/{id}/publish`. |
| `docker-compose.yml`, `frontend/Dockerfile` | `EMAIL_VERIFICATION_URL_DOCKER` for links in Docker emails; optional `VITE_RECAPTCHA_SITE_KEY` build arg. |
