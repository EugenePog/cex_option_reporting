.PHONY: help install dev db-up db-down migrate revision init-db seed collect-snapshot-loop collect-loop backfill pipeline pipeline-silver pipeline-gold debug-expiries web test lint format typecheck

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

seed:  ## Load core/settings CSVs from seed/ into the DB (upsert by id)
	python -m app.cli seed

collect-snapshot-loop:  ## Snapshot scheduler (balance/positions/margin/greeks) — runs at SNAPSHOT_TIMES
	python -m app.cli snapshot --loop

collect-loop:  ## History scheduler (fills/closed/bills) — once/day at INGEST_HOUR_UTC, limited depth
	python -m app.cli history --loop

backfill:  ## Collect the full available history depth from the exchange (one-off)
	python -m app.cli backfill

pipeline:  ## Run both transform stages (bronze->silver then silver->gold)
	python -m app.cli pipeline --stage all

pipeline-silver:  ## Run only bronze->silver
	python -m app.cli pipeline --stage silver

pipeline-gold:  ## Run only silver->gold
	python -m app.cli pipeline --stage gold

debug-expiries:  ## Trace closed/expired options across bronze/silver/gold (args: D1=YYYY-MM-DD D2=YYYY-MM-DD)
	scripts/debug_expiries.sh $(D1) $(D2)

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
