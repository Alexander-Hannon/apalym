# Agents

## Cursor Cloud specific instructions

### Architecture overview

Apalym is a document intelligence (RAG) platform. The codebase is a monorepo with three application services plus two infrastructure services, all orchestrated via Docker Compose. See `README.md` for the standard development commands (`make dev`, `make down`, `make logs`).

| Service | Tech | Port | Role |
|---------|------|------|------|
| **web** | Next.js 16 (TypeScript) | 3000 | Frontend UI |
| **api** | FastAPI (Python 3.12) | 8000 | Core REST API — upload, chunk, embed, index, search, RAG Q&A |
| **ai** | FastAPI (Python 3.12) | 8001 | Stub/echo microservice (only used by `/v1/demo`) |
| **db** | PostgreSQL 16 | 5432 | Relational database |
| **redis** | Redis 7 | 6379 | Cache / message broker |

### Starting the dev environment

1. Copy `.env.example` to `.env` and fill in `GEMINI_API_KEY`, `PINECONE_API_KEY`, and `PINECONE_HOST` (these are available as environment secrets in the Cloud VM).
2. Start Docker daemon: `sudo dockerd &>/tmp/dockerd.log &` (wait ~3s for it to initialize). You may need to run `sudo chmod 666 /var/run/docker.sock` afterward.
3. Run `make dev` (equivalent to `docker compose up --build`). Use `-d` flag for detached mode.

### Gotchas

- The Docker container for `web` creates `.next/` owned by root inside the bind-mount. If you need to run `npm run build` or other Next.js commands locally (outside Docker), first fix permissions: `sudo chown -R ubuntu:ubuntu apps/web/`.
- Python dependencies for `api` and `ai` are installed inline in the Dockerfiles (`infra/docker/api.Dockerfile`, `infra/docker/ai.Dockerfile`); there are no `requirements.txt` files.
- The `NEXT_PUBLIC_API_BASE_URL` in `.env` must point to the API service (default port 8000) for the web frontend to reach the API from the browser.

### Lint / build / test

- **Web lint**: `cd apps/web && npm run lint` (ESLint). Pre-existing `@typescript-eslint/no-explicit-any` warnings exist in `app/page.tsx`.
- **Web build**: `cd apps/web && npm run build` (Next.js production build).
- **Web typecheck**: `cd apps/web && npx tsc --noEmit`.
- No automated test suites exist in the repo at this time.

### Health checks

- API: `curl localhost:8000/health`
- AI: `curl localhost:8001/health`
- Web: `curl localhost:3000` (returns HTML)
