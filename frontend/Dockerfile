# Frontend image: build the Vite app, then serve it with nginx.
# nginx also proxies /api to the backend container, so the browser talks to one origin.
# Build context is frontend/:  docker compose build frontend

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_URL=/api/v1
ARG VITE_RECAPTCHA_SITE_KEY=
ENV VITE_API_URL=$VITE_API_URL VITE_USE_MOCK=false VITE_RECAPTCHA_SITE_KEY=$VITE_RECAPTCHA_SITE_KEY
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
