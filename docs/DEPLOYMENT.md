# ArbScanner Deployment Guide

This guide walks you through deploying arbscanner to a production environment. arbscanner has two long-running processes you typically want running side-by-side:

- **`arbscanner scan`** — the actual scanner loop; polls markets, detects arbs, logs to SQLite, sends alerts
- **`arbscanner serve`** — the FastAPI web dashboard on port 8000

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS        | Linux (any recent distro) | Ubuntu 22.04+ / Debian 12+ |
| CPU       | 2 vCPU | 4 vCPU |
| RAM       | 2 GB | 4 GB |
| Disk      | 5 GB | 20 GB (SQLite grows over time) |
| Python    | 3.12 | 3.12 |
| Node.js   | 18+ (for `pmxtjs` sidecar) | 20+ |

The sentence-transformers model adds ~80 MB to the first-run install. The SQLite `opportunities` table grows roughly 1 MB per 10k logged rows.

---

## Deployment Option 1: Docker Compose (recommended)

The repository ships with a production `Dockerfile` (multi-stage, non-root, includes Node.js for the pmxt sidecar) and a `docker-compose.yml`.

### Step 1: Clone and configure

```bash
git clone https://github.com/bsr-0/arbscanner.git
cd arbscanner
cp .env.example .env
$EDITOR .env   # fill in credentials (see reference below)
```

### Step 2: Build and start

```bash
docker compose up -d --build
```

This brings up the `scanner` service with:
- Port 8000 exposed (web dashboard)
- Named volumes `arbscanner_data` and `arbscanner_db` for persistence
- `restart: unless-stopped` policy
- Healthcheck probing `http://localhost:8000/api/stats` every 30s

### Step 3: Run the matcher one-shot

Before the scanner can find anything, you need to build the matched-pairs cache:

```bash
docker compose run --rm scanner arbscanner match
```

This takes a few minutes the first time (downloads sentence-transformers, fetches all active markets from both exchanges, runs embeddings, optionally calls Claude). Subsequent runs are incremental.

### Step 4: Verify

```bash
# Container logs
docker compose logs -f scanner

# Hit the API
curl http://localhost:8000/api/stats
curl 'http://localhost:8000/api/opportunities?hours=1'

# Readiness probe
curl http://localhost:8000/ready
```

Open the HTML dashboard at `http://localhost:8000/dashboard`.

### Step 5: Rematch on a schedule

Markets appear and close daily. Schedule a weekly rematch via cron on the host:

```cron
0 3 * * 1 cd /opt/arbscanner && docker compose run --rm scanner arbscanner match
```

---

## Deployment Option 2: Systemd on bare metal

If you don't want Docker, run directly via systemd.

### Step 1: Create a dedicated user

```bash
sudo useradd -r -m -d /opt/arbscanner -s /bin/false arbscanner
```

### Step 2: Clone and install

```bash
sudo -u arbscanner bash <<'EOF'
cd /opt/arbscanner
git clone https://github.com/bsr-0/arbscanner.git .
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv sync
uv pip install -e .
cp .env.example .env
EOF
sudo $EDITOR /opt/arbscanner/.env
sudo chown arbscanner:arbscanner /opt/arbscanner/.env
sudo chmod 600 /opt/arbscanner/.env
```

### Step 3: Install the Node sidecar globally

```bash
sudo npm install -g pmxtjs
```

### Step 4: Install the systemd unit

```bash
sudo cp /opt/arbscanner/scripts/systemd/arbscanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arbscanner
```

### Step 5: Run the matcher once

```bash
sudo -u arbscanner /opt/arbscanner/.venv/bin/arbscanner match
```

### Step 6: Tail logs

```bash
sudo journalctl -u arbscanner -f
```

The provided unit file has security hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, and `ReadWritePaths=/opt/arbscanner`.

---

## Reverse Proxy (nginx)

If you're exposing the web dashboard to the public internet, put it behind nginx with TLS.

```nginx
server {
    listen 80;
    server_name arbscanner.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name arbscanner.example.com;

    ssl_certificate     /etc/letsencrypt/live/arbscanner.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/arbscanner.example.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    # Future: WebSocket upgrade for live dashboard updates
    location /ws {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
    }
}
```

Get a cert with `certbot --nginx -d arbscanner.example.com`.

---

## Environment Variables Reference

All variables are optional unless marked **required**. Settings are read from `.env` in the project root (loaded by `python-dotenv`).

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `POLYMARKET_PRIVATE_KEY` | string | — | Required only for live trading (not scanning) |
| `KALSHI_API_KEY` | string | — | Required only for live trading |
| `KALSHI_PRIVATE_KEY` | string | — | Kalshi RSA private key for signed requests |
| `ANTHROPIC_API_KEY` | string | — | Required for LLM-assisted matching; without it, candidates above similarity 0.7 are auto-accepted |
| `TELEGRAM_BOT_TOKEN` | string | — | Enable Telegram alerts |
| `TELEGRAM_CHAT_ID` | string | — | Destination chat for Telegram alerts |
| `DISCORD_WEBHOOK_URL` | string | — | Enable Discord alerts |
| `STRIPE_SECRET_KEY` | string | — | Enable paid-tier checkout flow |
| `STRIPE_WEBHOOK_SECRET` | string | — | Validate Stripe webhooks |
| `STRIPE_PRICE_ID` | string | — | Product price for `/api/stripe/checkout` |
| `ARBSCANNER_PUBLIC_URL` | string | `http://localhost:8000` | Absolute base URL for Stripe success/cancel redirects |
| `ARBSCANNER_SECRET_KEY` | string | `dev-secret-key` | Session/cookie signing key — **change in production** |
| `ARBSCANNER_LOG_LEVEL` | string | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `ARBSCANNER_LOG_JSON` | bool | `0` | `1` for JSON-formatted logs (stdout) |

Scanner tuning (edit `src/arbscanner/config.py` or override via env before process start):

| Setting | Default | Purpose |
|---------|---------|---------|
| `refresh_interval` | 30 | Scan cycle interval (seconds) |
| `edge_threshold` | 0.01 | Minimum net edge to surface (1%) |
| `alert_threshold` | 0.02 | Minimum net edge to trigger alerts (2%) |
| `max_workers` | 8 | ThreadPoolExecutor size for parallel fetches |
| `rate_limit_per_sec` | 10.0 | Shared pmxt call rate cap |
| `retry_attempts` | 3 | Retries per failing request |

---

## Observability

### Logs

**Docker:** `docker compose logs -f scanner`

**Systemd:** `sudo journalctl -u arbscanner -f`

For JSON structured logs (e.g. to ship to Loki/ELK):

```bash
ARBSCANNER_LOG_JSON=1 arbscanner serve
```

### Health probes

```bash
curl http://localhost:8000/health   # liveness: always 200 if process is up
curl http://localhost:8000/live     # alias for /health (k8s)
curl http://localhost:8000/ready    # readiness: 200 if DB accessible AND matched pairs cache non-empty, else 503
```

Kubernetes probe example:

```yaml
livenessProbe:
  httpGet: { path: /live, port: 8000 }
  periodSeconds: 30
readinessProbe:
  httpGet: { path: /ready, port: 8000 }
  periodSeconds: 10
  failureThreshold: 3
```

### Metrics

The in-process metrics registry (`src/arbscanner/metrics.py`) exposes counters, gauges, and histograms for scan cycles, order book fetches, opportunities found, and rate limit waits. Future work: a `/metrics` endpoint exporting Prometheus text format (the registry already supports `.export_text()`).

---

## Database Operations

### Backups

arbscanner ships with a `backup` subcommand that uses SQLite's native online backup API — safe to run while the scanner is writing.

```bash
# Create a timestamped snapshot in ./backups/
arbscanner backup create

# List snapshots
arbscanner backup list

# Keep only the newest 10
arbscanner backup prune --keep 10

# Restore from a specific file (scanner should be stopped first)
systemctl stop arbscanner
arbscanner backup restore --file ./backups/arbscanner-20260410120000.db
systemctl start arbscanner
```

Schedule daily backups via cron:

```cron
0 4 * * * /opt/arbscanner/.venv/bin/arbscanner backup create && /opt/arbscanner/.venv/bin/arbscanner backup prune --keep 14
```

### Retention

Opportunities accumulate fast. Prune the table periodically:

```bash
# Delete opportunities older than 30 days and VACUUM
arbscanner backup prune-opps --days 30
```

---

## Scaling Considerations

arbscanner is single-instance by design for v1. If you need more capacity:

**Writer contention.** SQLite allows one writer at a time. Running two `arbscanner scan` processes against the same `arbscanner.db` will deadlock. Use a single scanner.

**Read scaling.** The web server can handle many concurrent readers. To horizontally scale the API, point multiple `arbscanner serve` instances at a shared SQLite file (read-only is safe) or migrate to Postgres.

**Migration to Postgres.** The schema is portable. Replace `sqlite3` with `psycopg` in `db.py`, change the connection URL, run migrations. No application code changes beyond the DB driver are required.

**Rate limit budget.** If you raise `max_workers` too high without raising `rate_limit_per_sec`, workers will stall on the rate limiter. The limiter is global, so setting it correctly caps total throughput across all workers.

**Model download.** sentence-transformers downloads the MiniLM model to `~/.cache/huggingface/hub/`. In Docker, mount this as a volume to avoid re-downloading on image rebuilds:

```yaml
volumes:
  - arbscanner_hf_cache:/home/arbscanner/.cache/huggingface
```

---

## Security

**Never commit `.env`.** It's in `.gitignore`; verify before every push.

**Rotate API keys.** Anthropic, Stripe, and Telegram keys should be rotated periodically. Update `.env` and `systemctl restart arbscanner`.

**Firewall.** Expose only 80/443 to the public internet. Keep 8000 bound to localhost (nginx handles the edge).

**Non-root.** Both the Docker image and the systemd unit run as the unprivileged `arbscanner` user. Don't override this.

**Authentication.** The web dashboard currently has **no per-user authentication**. Stripe checkout + webhook endpoints are wired and working, but tier enforcement is still driven by the process-wide `ARBSCANNER_TIER` env var — there is no persistence layer mapping paying customers to pro access. Webhooks currently only log `checkout.session.completed` and `customer.subscription.deleted` events. If you're deploying publicly, either:
1. Put it behind nginx basic auth / oauth2-proxy
2. Keep it on a private network
3. Accept that every visitor gets the tier set by `ARBSCANNER_TIER`

**Stripe configuration.** `/api/stripe/checkout` creates real Checkout Sessions; `/api/stripe/webhook` validates the `stripe-signature` header against `STRIPE_WEBHOOK_SECRET`. Set `ARBSCANNER_PUBLIC_URL` to the externally-reachable base URL of this deployment so post-checkout redirects land back on the right host.

---

## Upgrade Procedure

### Docker

```bash
cd /opt/arbscanner
git pull
docker compose up -d --build
```

### Systemd

```bash
cd /opt/arbscanner
sudo -u arbscanner git pull
sudo -u arbscanner .venv/bin/uv sync
sudo systemctl restart arbscanner
sudo journalctl -u arbscanner -f
```

Always run tests on a staging environment before upgrading production.

---

## Disaster Recovery

### Lost database

Restore from the most recent backup:

```bash
arbscanner backup list
arbscanner backup restore --file ./backups/arbscanner-YYYYMMDDHHmmss.db
```

### Corrupted `data/matched_pairs.json`

Delete and rerun the matcher:

```bash
rm data/matched_pairs.json
arbscanner match --rematch
```

This takes a few minutes but is fully deterministic given the same market inputs.

### Scanner stuck / stale dashboard

Restart and verify:

```bash
sudo systemctl restart arbscanner
curl http://localhost:8000/ready
```

If `/ready` returns 503, check logs — the most common causes are missing matched pairs and an inaccessible database file.

---

## CI/CD

The repository includes two GitHub Actions workflows:

- **`.github/workflows/ci.yml`** — runs `pytest` and `ruff` on every push and PR
- **`.github/workflows/docker.yml`** — builds and publishes multi-arch Docker images to `ghcr.io/bsr-0/arbscanner` on every `v*.*.*` tag push

Tagging a release is enough to cut a new image:

```bash
git tag v0.3.0
git push origin v0.3.0
```

Your production deploy can then pin to the tagged image:

```yaml
services:
  scanner:
    image: ghcr.io/bsr-0/arbscanner:v0.3.0
```
