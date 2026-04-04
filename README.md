# INbox

Personal PoC for an email-driven job application pipeline. See [DESIGN.md](DESIGN.md) for phases and architecture.

## Repository layout

| Path | Role |
|------|------|
| `src/jobinbox/config.py` | Settings from env (paths, limits, Gmail query) |
| `src/jobinbox/ingestion/` | Provider adapters (Gmail now; IMAP/API later) |
| `src/jobinbox/cli.py` | CLI: `python -m jobinbox fetch` |
| `index.html` | Browser inbox viewer (Gmail JS client) |
| `config.example.js` | Template for web credentials — copy to `config.local.js` (gitignored) |

Secrets never belong in git: use `credentials.json` / `token.json` for Python, `config.local.js` for the web demo.

---

## Python CLI (Gmail API)

Uses an OAuth client of type **Desktop app** and `credentials.json` at the repo root (or `JOBINBOX_CREDENTIALS`).

1. [Google Cloud Console](https://console.cloud.google.com/) → your project → **APIs & Services** → enable **Gmail API**.
2. **Credentials** → **Create credentials** → **OAuth client ID** → application type **Desktop app** → download JSON.
3. Save as `credentials.json` in this directory (gitignored), or set `JOBINBOX_CREDENTIALS` to its path.

```bash
cd INbox   # or your clone root containing pyproject.toml
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
python -m jobinbox fetch --pretty --limit 10
```

First run opens a browser to sign in; `token.json` is written (gitignored). Optional env vars: see `env.example` and `jobinbox.config.Settings`.

---

## Web inbox viewer (`index.html`)

Uses the **browser** OAuth flow — you need a **separate** OAuth client of type **Web application** (not the Desktop client used by Python). Same GCP project is fine.

### Google Cloud setup (web)

1. **Gmail API** enabled (same as above).
2. **Credentials** → **OAuth client ID** → **Web application**.
3. **Authorized JavaScript origins** (must match how you open the page), e.g.  
   `http://127.0.0.1:8080` and `http://localhost:8080`.
4. **Authorized redirect URIs** — add the same origins (and optionally `http://127.0.0.1:8080/` with a trailing slash if Google still complains).
5. **OAuth consent screen**: while status is **Testing**, add your Google account under **Test users**.
6. Create an **API key** (same project). Restrict it (e.g. HTTP referrers for `http://127.0.0.1:8080/*`) before any public deployment.

### Local config

```bash
cp config.example.js config.local.js
# Edit config.local.js: set clientId (Web client) and apiKey
```

Never commit `config.local.js`.

### Serve over HTTP

Do not open `index.html` as a `file://` URL; use a local origin so OAuth matches your Console settings.

```bash
python3 -m http.server 8080
```

Open `http://127.0.0.1:8080/index.html` (or the same host/port you registered).

---

## Pushing to GitHub

- Confirm **no secrets** are tracked: `credentials.json`, `token.json`, `config.local.js`, `.env` should only exist locally (see `.gitignore`).
- If OAuth client IDs or API keys were ever committed or shared, **rotate** them in Google Cloud Console and use the new values only in local files.
- Optional: add a GitHub repo description pointing to `DESIGN.md` for roadmap context.

---

## Environment (Python)

Copy `env.example` to `.env` and load it with your shell if you use non-default paths; variables are optional — see `jobinbox.config.Settings`.
