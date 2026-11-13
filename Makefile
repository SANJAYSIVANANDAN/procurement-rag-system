.PHONY: install lint format typecheck imports test check up down logs ps

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy src/covenantiq

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
