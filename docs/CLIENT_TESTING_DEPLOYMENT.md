# Selected client testing — deployment checklist

This document describes how to run Apalym for **isolated client testing** (upload → auto insights → follow-up Q&A) and what operators must configure. It matches the narrow **client-test access key** model implemented in this sprint.

## Architecture reminder

- **web** (Next.js): browser UI; calls **api** using the public API endpoint configured for the web build (see `.env.example`).
- **api** (FastAPI): uploads to tenant-scoped disk paths, embeds/indexes into Pinecone **namespace = tenant_id**, serves `/v1/insights` and `/v1/ask`.
- **db** / **redis**: present in compose; RAG path does not require DB for tenant mapping in this mode.

## Required environment variables

### Always

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEY` | Google GenAI / embeddings |
| `PINECONE_API_KEY`, `PINECONE_HOST`, `PINECONE_INDEX` | Vector index |
| Web public API endpoint | Browser-reachable base URL for the API (see the web section of `.env.example`). |

### Model (optional)

| Variable | Default |
|----------|---------|
| `GEMINI_CHAT_MODEL` | `gemini-3.0-flash` |

### Tenant isolation — client testing keys (recommended for external testers)

| Variable | Purpose |
|----------|---------|
| `APALYM_CLIENT_TEST_KEYS` | Semicolon-separated `access_key:tenant_id:Display Label` entries. **Third part (label) is optional**; if omitted, label defaults to `tenant_id`. |

When `APALYM_CLIENT_TEST_KEYS` is **non-empty**:

- Every `/v1/*` route **except** `GET /v1/auth/config` and `POST /v1/auth/login` requires `Authorization: Bearer <access_key>` (including `GET /v1/auth/session` for validating a stored key).
- The API **ignores** `x-tenant-id` from clients; the tenant is always derived from the validated key.
- Pinecone namespaces and on-disk storage paths are scoped to that tenant.

When it is **empty** (not set or blank):

- The API runs **open demo mode**: a single shared tenant (`DEMO_TENANT_ID`, default `demo-tenant`). Suitable only for trusted internal demos.

| Variable | Purpose |
|----------|---------|
| `DEMO_TENANT_ID` | Shared tenant id in open demo mode (default `demo-tenant`). |

### CORS

| Variable | Default |
|----------|---------|
| `CORS_ALLOW_ORIGINS` | See `.env.example` (comma-separated browser origins). |

Use a comma-separated list for production web origins, e.g. `https://app.customer.com,https://staging.customer.com`.

## Startup commands

From repo root (after copying `.env` from `.env.example` and filling secrets):

1. Start Docker if needed, then:
   - `make dev`  
   or  
   - `docker compose up --build`

2. Health checks (substitute your deployed hostnames and ports):

   - API: `GET /health` on the API service.
   - Web: `GET /` on the web service (expect HTTP 200).

## Demo / test accounts

There are no built-in users. Operators **mint access keys** and map them to tenant ids via `APALYM_CLIENT_TEST_KEYS`.

Example (two clients):

```bash
APALYM_CLIENT_TEST_KEYS='sk_acme_001:tenant-acme:Acme Corp;sk_contoso_002:tenant-contoso:Contoso'
```

Distribute **only** the `sk_*` string to the matching client. Anyone with the key can act as that tenant until the key is rotated.

## Known limitations (explicit)

- **Browser session storage**: The web app keeps the access key in **`sessionStorage`**. It is cleared when the tab/window is closed, is **not** shared across tabs, and is readable by any script on the **same origin** as the web app—use a dedicated pilot origin and standard XSS hygiene for external testers.
- **Not full SSO/OIDC**: Bearer access keys are shared secrets, not user accounts with password policies or MFA.
- **Workspace = tenant**: There is no separate workspace hierarchy in this sprint; `workspace_id` in auth responses equals `tenant_id`.
- **Key compromise = tenant compromise**: Rotate keys by changing `APALYM_CLIENT_TEST_KEYS` and restarting **api**.
- **Storage is local volume** in default compose: multi-replica deployments need shared object storage (not part of this sprint).

## Rollback

1. Revert or redeploy the previous container image / git revision.
2. Remove or blank `APALYM_CLIENT_TEST_KEYS` to return to open demo mode (only if acceptable for your environment).
3. If Pinecone data was polluted during a bad test, delete vectors under the affected **namespace** (`tenant_id`) from the Pinecone console or via API.

## Observability

Structured logs (stdout) include, where applicable:

- `request_id`, `tenant_id`, `doc_id` / `doc_id_filter`, `retrieved_k`, `top_k`
- Insights: `insight_module_status` with `module_llm_status` of `generated`, `fallback`, `failed`, or `insufficient_evidence`
- LLM retries: `llm_call_retry` with `backoff_seconds`

Client-visible JSON **does not** include internal `error` strings on insight modules; failures are logged server-side.

## Web client behavior

- If `GET /v1/auth/config` returns `client_test_auth_enabled: true`, the UI shows a **sign-in** gate until a successful `/v1/auth/login`.
- The access key is kept in **sessionStorage** and sent as `Authorization: Bearer …` on each API call.
- **Diagnostics** (sidebar): optional panel with last `request_id` and tenant id for support — not intended for end-user demos.
