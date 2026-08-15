.PHONY: help install dev db-up db-down migrate revision init-db collect collect-loop backfill pipeline web test lint format typecheck

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime + dev deps into the active environment
	pip install -e ".[dev]"

db-up:  ## Start local Postgres (docker compose)
	docker compose up -d

db-down:  ## Stop local Postgres
	docker compose down

migrate:  ## Apply all Alembic migrations
	alembic upgrade head

revision:  ## Create a new migration: make revision m="message"
	alembic revision -m "$(m)"

init-db:  ## Create schemas + tables from ORM metadata (dev; or use `make migrate`)
	python -m app.cli init-db

collect:  ## Run ONE daily collection pass (snapshot + recent fills) into bronze
	python -m app.cli collect

collect-loop:  ## Run the daily scheduler (fires at INGEST_HOUR_UTC) — same as the pm2 collector
	python -m app.cli collect --loop

backfill:  ## Collect the full available history depth from the exchange (one-off)
	python -m app.cli backfill

pipeline:  ## Run one bronze->silver->gold pass
	python -m app.cli pipeline

web:  ## Run the web app (dev, autoreload)
	uvicorn app.web.main:app --reload --host 0.0.0.0 --port 8000

test:  ## Run tests
	pytest -q

lint:  ## Lint
	ruff check app tests

format:  ## Format
	black app tests && ruff check --fix app tests

typecheck:  ## Type-check
	mypy app
