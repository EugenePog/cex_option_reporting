.PHONY: help install dev db-up db-down migrate revision init-db seed collect-snapshot-loop collect-loop backfill pipeline pipeline-silver pipeline-gold debug-expiries debug-payoff web worker alerts-dry-run alerts-test test lint format typecheck

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

seed:  ## Load seed/common + seed/<APP_ENV> (default dev; make seed ENV=test) into core, matched on keys
	python -m app.cli seed $(if $(ENV),--env $(ENV),)

collect-snapshot-loop:  ## Snapshot scheduler (balance/positions/margin/greeks + BTC-USD 1m index candles) — SNAPSHOT_TIMES_UTC, default hourly
	python -m app.cli snapshot --loop

collect-loop:  ## History scheduler (fills/closed/bills + BTC-USD 1m index candles) — INGEST_TIME_UTC, default hourly, limited depth
	python -m app.cli history --loop

backfill:  ## Full history depth from the exchange, then BTC-USD 1m index candles from the earliest position (one-off)
	python -m app.cli backfill

pipeline:  ## Run both transform stages (bronze->silver then silver->gold)
	python -m app.cli pipeline --stage all

pipeline-silver:  ## Run only bronze->silver
	python -m app.cli pipeline --stage silver

pipeline-gold:  ## Run only silver->gold
	python -m app.cli pipeline --stage gold

debug-expiries:  ## Trace closed/expired options across bronze/silver/gold (args: D1=YYYY-MM-DD D2=YYYY-MM-DD)
	scripts/debug_expiries.sh $(D1) $(D2)

debug-payoff:  ## Diagnose report ③ (payoff): expiry coverage + settlement-price gaps (arg: ULY=BTC-USD)
	scripts/debug_payoff.sh $(ULY)

web:  ## Run the web app (dev, autoreload)
	uvicorn app.web.main:app --reload --host 0.0.0.0 --port 8000

worker:  ## Telegram alerts for graph ① boxes — check every ALERTS_INTERVAL_SECONDS (pm2 `worker`)
	python -m app.cli worker --loop

alerts-dry-run:  ## Print the Telegram alerts that would be sent now (sends and stores nothing)
	python -m app.cli worker --dry-run

alerts-test:  ## Send one test message to the Telegram chat (checks TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
	python -m app.cli worker --test

test:  ## Run tests
	pytest -q

lint:  ## Lint
	ruff check app tests

format:  ## Format
	black app tests && ruff check --fix app tests

typecheck:  ## Type-check
	mypy app
