# Apalym Monorepo

## Run (Dev)
docker compose up --build

## Services
- web: http://localhost:3000
- api: http://localhost:8000/health
- ai:  http://localhost:8001/health


## Local Boot

```bash
cp .env.example .env
docker compose down -v
docker compose up --build
