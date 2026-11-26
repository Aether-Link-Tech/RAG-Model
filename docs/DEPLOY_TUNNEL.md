# Public link with Cloudflare Tunnel

The whole stack keeps running on one Windows laptop (Docker, Ollama, documents). Cloudflare Tunnel gives it a
public HTTPS address without opening ports on the router. The laptop must stay on, awake and online; when it
sleeps or shuts down, the public link shows a Cloudflare error until it is back.

```
Visitor ──HTTPS──▶ Cloudflare ◀══ outgoing tunnel ══ cloudflared (laptop) ──▶ Caddy :443 ──▶ nginx ──▶ backend
```

Privacy: AI models and documents stay on the laptop, but Cloudflare terminates HTTPS, so page traffic passes
through Cloudflare. Fine for demos; for confidential data use your own server (or the university portal).

## What the project already does for the tunnel

- Caddy trusts `CF-Connecting-IP` only from private (local) peers and forwards the real visitor IP
  (`docker/caddy/Caddyfile`), so per-IP limits and the audit log see real visitors.
- Every port is bound to `127.0.0.1` (`docker-compose.yml`): the only way in from outside is the tunnel, so
  nobody on the network can fake the visitor-IP header or reach Postgres, Redis or Chroma.
- Guest free questions are counted per browser (anonymous cookie) with a higher per-IP ceiling, so a whole
  classroom on one Wi-Fi does not share 5 questions (`PUBLIC_ASK_PER_DAY`, `PUBLIC_ASK_PER_IP_DAY`).

## 1. Before going public (once)

In `.env`:

```bash
SECRET_KEY=<a new long random value>        # python -c "import secrets; print(secrets.token_urlsafe(48))"
ENV=production                              # optional: stricter checks
```

Ollama, Docker Desktop and the stack must be running (`docker compose up -d`), and https://localhost must work.

## 2. Install cloudflared (once)

```powershell
winget install --id Cloudflare.cloudflared
cloudflared --version
```

## 3. Start a quick tunnel (no account, for demos)

```powershell
cloudflared tunnel --url https://localhost:443 --no-tls-verify --http-host-header localhost --origin-server-name localhost
```

It prints an address like `https://random-words.trycloudflare.com`. Keep this window open: closing it ends the
tunnel. The address changes every time the tunnel is restarted.

`--no-tls-verify` is needed because Caddy uses its local certificate for `localhost`; the connection from
cloudflared to Caddy never leaves the laptop.

## 4. Make verification emails point to the public address

In `.env`, then recreate the backend:

```bash
EMAIL_VERIFICATION_URL_DOCKER=https://random-words.trycloudflare.com/verify-email
```

```powershell
docker compose up -d --no-build --force-recreate backend
```

Emails still go to Mailpit (http://localhost:8025) until a real SMTP service (for example Brevo or Resend) is
set in `SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM`.

## 5. Keep the laptop online

- Plugged in. Settings → System → Power: Sleep **Never**.
- Control Panel → Power Options → "Choose what closing the lid does": **Do nothing** (then the lid can be closed).
- Docker Desktop → Settings → General: **Start Docker Desktop when you sign in**.

## 6. Permanent address (later)

A named tunnel keeps the same address: a free Cloudflare account and a domain added to Cloudflare.

```powershell
cloudflared tunnel login
cloudflared tunnel create aether-link
cloudflared tunnel route dns aether-link aether.<your-domain>
cloudflared service install          # starts with Windows
```

Point it at `https://localhost:443` with `noTLSVerify: true`, `httpHostHeader: localhost` and
`originServerName: localhost` in `%USERPROFILE%\.cloudflared\config.yml`.

## Check that it works

- Open the public address on a phone using mobile data (not the same Wi-Fi).
- Ask a guest question on the landing page; the backend log shows the phone's public IP, not `172.x`:
  `docker logs --tail 5 rag_backend`.
