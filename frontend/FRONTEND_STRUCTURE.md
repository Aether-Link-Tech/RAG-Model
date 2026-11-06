# Frontend structure guide

This folder structure was added as a starting point. Existing project files and code were kept unchanged.

## Folder responsibilities

- `src/pages/`: full route-level screens (Login, Signup, Dashboard, Chat, Documents).
- `src/components/`: reusable UI pieces (buttons, inputs, modals, navigation).
- `src/layouts/`: shared page frames (authentication and dashboard layouts).
- `src/services/`: API calls to the backend (for example, `api.ts`).
- `src/hooks/`: reusable React hooks.
- `src/types/`: shared TypeScript types and interfaces.
- `src/assets/images/`: images imported by components/pages.
- `src/assets/icons/`: local icon assets.
- `public/images/`: static images referenced by URL, such as `/images/logo.png`.

## Existing files preserved

- `src/App.tsx`
- `src/main.tsx`
- `src/index.css`
- `src/pages/login.tsx`
- `src/pages/signup.tsx`
- `package.json`, lockfile, and Vite/TypeScript configuration

No routes, page code, styles, package dependencies, or backend files were changed. Add files to the new folders as you need them; the `.gitkeep` files simply keep currently-empty folders visible in version control.
