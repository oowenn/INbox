# INbox

Dashboard-first product codebase for an email-driven job application tracker. This README assumes you run everything with **Docker Compose**.

Core workflow:

1. Pull newest Gmail messages into local SQLite (`jobinbox.db`) for the active user.
2. Process unclassified cached emails in parallel with the configured LLM backend.
3. Inspect transitions in the interactive Sankey chart and drill into `(company, role)` pairs.
4. Optionally clear only results or wipe cached user emails/results to reset the dashboard state.

See [DESIGN.md](DESIGN.md) for roadmap and architecture.

## Prerequisites

- **Docker** and **Docker Compose**
- **LLM backend (pick one):**
  - **Ollama** on the host (default `http://127.0.0.1:11434` locally, `http://host.docker.internal:11434` in Compose), with a model pulled. **`JOBINBOX_OLLAMA_MODEL`** defaults to **`qwen3:1.7b`** (`ollama pull qwen3:1.7b`). Override with another tag if you prefer.
  - **OpenAI** (optional): set **`JOBINBOX_LLM_PROVIDER=openai`** and **`JOBINBOX_OPENAI_API_KEY`** or **`OPENAI_API_KEY`**. The container needs outbound HTTPS to `api.openai.com` (or your **`JOBINBOX_OPENAI_BASE_URL`**). Default model **`gpt-4o-mini`**; override with **`JOBINBOX_OPENAI_MODEL`**.
- A **Google Cloud** project with **Gmail API** enabled and OAuth consent configured (Testing + your account as a **Test user** is enough for personal use)

## Files in the repo root (host)

The compose file bind-mounts this directory to **`/app/host`** in the container. Put secrets here (all gitignored):

| File | Required | Purpose |
|------|----------|---------|
| **`desktop_credentials.json`** | **Yes** | **Desktop app** OAuth client JSON — used for Gmail in the container. |
| **`webapp_credentials.json`** | No | Web client JSON; not used for Gmail here. Optional (e.g. future browser demos). `GET /api/health` reports if it exists. |
| **`token.json`** | Created after first OAuth | Saved on the host after you complete sign-in once. |
| **`jobinbox.db`** | Created automatically | Local cache of fetched emails and latest classification result per Gmail message. |

## Google Cloud: OAuth clients

1. [Google Cloud Console](https://console.cloud.google.com/) → enable **Gmail API**.
2. **Credentials** → **OAuth client ID** → type **Desktop app** → download JSON → save as **`desktop_credentials.json`** in this directory.
3. (Optional) Create a **Web application** client → save as **`webapp_credentials.json`** if you want it on disk for later; this stack does not use it for Gmail.

If Google shows **`redirect_uri_mismatch`** during the steps below, use a **Desktop** client for `desktop_credentials.json`, or register **`http://localhost:8090/`** (and optionally `http://127.0.0.1:8090/`) on a Web client.

## Commands (from this directory)

```bash
# 1) Build (use --no-cache after pulling code changes)
docker compose build --no-cache app

# 2) First-time Gmail OAuth — publishes port 8090 for the callback
docker compose run --rm --service-ports app python -m jobinbox fetch --limit 1 --pretty
# Open the printed URL, sign in; token.json appears in this directory.

# 3) Run API + UI
docker compose up --build
```

Open **http://127.0.0.1:8000**.

- Use **Pull newest** to fetch Gmail messages into the cache.
- Use **Process emails** to classify up to *N* unprocessed emails (streamed progress + parallel workers).
- Use **Clear results only** to remove classifications while keeping cached emails.
- Use **Wipe cached emails + results** to remove this user's cache and linked result rows.
- Use the **Application Flow (Sankey)** panel to inspect transitions and click a branch to list contributing `(company, role)` pairs.

### Optional: shell or one-off commands in the container

```bash
docker compose run --rm app python -m jobinbox fetch --pretty --limit 10
```

(Requires existing **`token.json`**. For OAuth again, add **`--service-ports`** as in step 2.)

### Health check

```bash
curl -s http://127.0.0.1:8000/api/health
```

## Environment variables

Override in `docker-compose.yml`, a `.env` file beside it, or `export` before `docker compose up`. Defaults are also documented in `env.example` and `src/jobinbox/config.py`.

| Variable | Role |
|----------|------|
| `JOBINBOX_PROJECT_ROOT` | Set in image to `/app/host` (bind mount). |
| `JOBINBOX_DESKTOP_CREDENTIALS` | Default in compose: `/app/host/desktop_credentials.json` |
| `JOBINBOX_WEBAPP_CREDENTIALS` | Default: `/app/host/webapp_credentials.json` |
| `JOBINBOX_TOKEN` | Default: `/app/host/token.json` |
| `JOBINBOX_DB_PATH` | Default: `<project_root>/jobinbox.db` (compose default `/app/host/jobinbox.db`) |
| `JOBINBOX_USER_ID` | Logical user partition for cache/results (default `local-user`) |
| `JOBINBOX_OLLAMA_BASE_URL` | Default: `http://host.docker.internal:11434` |
| `JOBINBOX_OLLAMA_MODEL` | Default: `qwen3:1.7b` |
| `JOBINBOX_OLLAMA_TIMEOUT_S` | Default: `180` (HTTP **read** timeout for `/api/chat`; connect stays 30s) |
| `JOBINBOX_LLM_PROVIDER` | `ollama` (default) or `openai` |
| `JOBINBOX_OPENAI_API_KEY` / `OPENAI_API_KEY` | OpenAI secret when provider is `openai` |
| `JOBINBOX_OPENAI_MODEL` | Default: `gpt-4o-mini` |
| `JOBINBOX_OPENAI_BASE_URL` | Default: `https://api.openai.com/v1` (compatible Chat Completions API) |
| `JOBINBOX_OPENAI_JSON_MODE` | Default: `1` — sends `response_format: json_object`; set `0` if your endpoint rejects it |
| `JOBINBOX_OAUTH_MODE` | Compose default: `manual` (print auth URL; no browser inside container) |
| `JOBINBOX_OAUTH_PORT` | Default: `8090` (stable redirect URI) |
| `JOBINBOX_OAUTH_BIND_ADDR` | Compose sets `0.0.0.0` so the callback is reachable through the published port |