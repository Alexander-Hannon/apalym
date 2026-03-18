# Apalym

Apalym is a document intelligence platform that allows users to upload PDFs and ask questions about their contents.  
Answers are generated with citations so users can trust what they’re seeing.

---

# Quick Start (Local Development)

### Prerequisites

- Docker Desktop
- WSL (Ubuntu recommended)
- Git

### Boot the App

```bash
git clone git@github.com:Alexander-Hannon/apalym.git
cd apalym
cp .env.example .env
make dev


# (The application will now start locally.)

-----------------------------------------------------------------

# Local Services

Once running, the following services will be available:

Service				URL

Web UI				http://localhost:3000
API				http://localhost:8000/health
AI Service			http://localhost:8001/health


------------------------------------------------------------------

# Development Commands

Start development environment:	make dev

View logs:			make logs

Stop containers:		make down

Clean containers and volumes:	docker compose down -v

------------------------------------------------------------------

# Environment Variable

Environment variables are defined in:	.env

To create your local environment file:	cp .env.example .env


(.env.example contains all required variables with placeholder values.)


-----------------------------------------------------------------------

# Repository Structure

apalym/
│
├─ apps/
│  ├─ web/        # Next.js frontend
│  ├─ api/        # FastAPI backend
│  └─ ai/         # AI microservice
│
├─ infra/
│  └─ docker/     # Dockerfiles
│
├─ docker-compose.yml
└─ Makefile


----------------------------------------------------------------------

# Troubleshooting


###Containers fail to start

Run a full reset:

			docker compose down -v
			make dev
###Port already in use

Stop existing containers:

			make down
