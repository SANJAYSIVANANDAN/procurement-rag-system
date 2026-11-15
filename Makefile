.PHONY: install lint format typecheck imports test check up down logs ps ui

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

imports:
	uv run lint-imports

test:
	uv run pytest tests/unit -q

check: lint typecheck imports test

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

# ---- evaluation (spec 28) ------------------------------------------------------------------------
# The offline run needs a migrated PostgreSQL with pgvector: set CIQ_EVAL_DATABASE_URL (it writes
# only under a throwaway user and removes it).

.PHONY: eval-dataset eval-offline eval-compare eval-baseline eval-full

eval-dataset:
	uv run python -m eval.datasets.generate_v1

eval-offline:
	uv run python -m eval.scripts.run_offline
	uv run python -m eval.scripts.compare_to_baseline

eval-compare:
	uv run python -m eval.scripts.compare_to_baseline

# Rewrite the committed baseline. Say why in the commit message (spec 28.6).
eval-baseline:
	uv run python -m eval.scripts.run_offline --write-baseline eval/baselines/offline_v1.0.0.json --note "$(NOTE)"

# Real providers and RAGAS: costs money, needs keys and `uv sync --group eval`.
eval-full:
	uv run --group eval python -m eval.scripts.run_full

ui:  ## the web UI on http://localhost:8501 (needs the API)
	uv run streamlit run frontend/app.py
