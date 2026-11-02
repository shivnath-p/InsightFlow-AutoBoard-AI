# InsightFlow AutoBoard AI (v2.1 starter, security-reviewed)

Upload a CSV/XLSX, ask a business question in plain language, get **verified** numbers,
charts, an editable versioned dashboard and PDF/XLSX/CSV exports.

## What is real in this code (and what is not)
Implemented and covered by tests (`backend/tests`, 69 tests):
- Register/login (JWT), per-organisation data isolation, audit log, rate limits
- CSV/XLSX upload with size/type limits, Parquet conversion, profiling
- Agent pipeline: plan -> validate -> DuckDB analysis -> pandas recomputation -> narrative,
  with step limits, one re-plan, clarifying questions and partial results
- Works without any API key (deterministic planner); Gemini is optional
- Every number in a narrative must match a verified fact (hallucinated numbers are rejected)
- Dashboards: evidence-bound components, versions, optimistic concurrency, restore
- Exports from the saved run snapshot (CSV/XLSX formula-injection safe, PDF)
- Security hardening (see below)

Not implemented yet (see the review PDF, section "Remaining work"): PostgreSQL + Alembic,
RAG, PPTX/DOCX export, background workers, SSE progress, orgs/invites/roles, malware scan,
PII masking, LangGraph checkpointing, MCP. This is a starter, not a production system.

## Security notes
- **Start-up:** with `ENVIRONMENT=production` the app refuses to start without a `SECRET_KEY` of
  32+ characters, hides `/docs`, and sends HSTS. `docker-compose.yml` defaults to production.
- **Auth:** JWT must carry `exp` and `sub`; login is rate limited per IP *and* per account;
  PBKDF2-SHA256 (600k iterations) with a timing-safe dummy hash for unknown emails.
- **Uploads:** content is checked against the extension, `.xlsx` zip bombs are refused before
  parsing, row/column/dataset-count limits, filenames are sanitised, and request bodies are
  size-capped while streaming (also for chunked requests).
- **Analytics:** the LLM never writes SQL. Requests are validated against the column list, SQL
  identifiers are quoted, values are bound, and DuckDB runs with external access disabled.
- **LLM output:** every number must match a verified fact; links/markup and over-long text are
  rejected. Set `LLM_SHARE_RESULTS=false` to keep aggregated results off the LLM entirely
  (the planner only ever sees column names, the narrator also sees group labels and values).
- **Containers:** non-root, read-only backend filesystem, all capabilities dropped, ports bound to
  localhost, frontend served by unprivileged nginx with a strict Content-Security-Policy.
- **CI:** `bandit`, `pip-audit` and `npm audit` run on every push.
- **Still your job:** put a TLS-terminating reverse proxy in front (and start uvicorn with
  `--proxy-headers --forwarded-allow-ips <proxy ip>` so rate limits see real client IPs), set
  `ALLOW_REGISTRATION=false` once users exist, back up `DATA_DIR`, and move to PostgreSQL +
  Alembic and a shared (Redis) rate limiter before running more than one worker.

## Run (Windows PowerShell or any shell)
```
copy .env.example .env          # then set SECRET_KEY (and GEMINI_API_KEY if you have one)
docker compose up --build
```
Frontend: http://localhost:5173. For the Swagger docs at http://localhost:8000/docs set
`ENVIRONMENT=development` in `.env`. The compose frontend is a production build; for hot
reload use `npm run dev` (below).

Without Docker:
```
cd backend; python -m venv .venv; .venv\Scripts\activate
pip install -r requirements-dev.txt; python -m pytest -q
uvicorn app.main:app --reload
cd ..\frontend; npm ci; npm run dev
```

## Changes in v2.1 (security review)
- New env vars (all optional, see `.env.example`): `ENVIRONMENT`, `ENABLE_DOCS`, `ENABLE_HSTS`,
  `LLM_SHARE_RESULTS`, `MAX_ROWS`, `MAX_UNZIPPED_MB`, `MAX_DATASETS_PER_ORG`,
  `MAX_JSON_BODY_BYTES`, `HEAVY_RATE_LIMIT_PER_MIN`.
- `docker compose` now needs `SECRET_KEY` in `.env` and serves the built frontend on port 5173.
- Tokens without an expiry are rejected, so tokens issued by an older build still work
  (they carry `exp`) but hand-made ones do not.

## Breaking changes from v1
- All routes except `/auth/*` and `/health` need `Authorization: Bearer <token>`.
- `/ask` now returns `{"run": {...}}` (facts, claims, warnings, trace).
- Dashboard `config` became a validated `spec` with `base_version`.
- The database schema changed. Delete the old `insightflow.db` and old uploads
  (there is no data migration; v1 data had no owner).
