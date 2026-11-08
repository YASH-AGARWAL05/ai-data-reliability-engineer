PYTHON := python3.12
UV := .tools/bin/uv
PY := .venv/bin/python
BIN := .venv/bin

.PHONY: setup dev test lint eval check mutation security e2e sandbox-image

setup:
	@if ! test -x $(UV); then $(PYTHON) -m pip install --target .tools uv==0.13.7; fi
	$(UV) sync --python $(PYTHON) --locked
	npm ci --legacy-peer-deps
	$(BIN)/pre-commit install
	$(MAKE) sandbox-image

sandbox-image:
	docker build -f docker/analytics/Dockerfile -t ai-reliability-dbt:phase2 .

dev:
	docker compose up -d --wait db
	PYTHONPATH=src $(BIN)/alembic upgrade head
	@PYTHONPATH=src $(PY) -m ai_data_reliability.service.worker & worker_pid=$$!; PYTHONPATH=src $(BIN)/uvicorn ai_data_reliability.service.api:app --host 127.0.0.1 --port 8000 & api_pid=$$!; trap 'kill $$worker_pid $$api_pid 2>/dev/null || true' EXIT INT TERM; npm run dev:web

test: sandbox-image
	docker compose up -d --wait db
	PYTHONPATH=src $(BIN)/alembic upgrade head
	@if test -d tests; then $(BIN)/pytest -q --cov=src --cov-report=term-missing; else echo 'No Python tests in Phase 0'; fi
	npm run test

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	@if test -d src; then set -e; $(BIN)/mypy src tests docker/analytics; $(BIN)/vulture src tests docker/analytics --min-confidence 80; $(BIN)/bandit -q -ll -r src docker/analytics; fi
	@if test -d src/ai_data_reliability/investigator -a -d src/ai_data_reliability/evaluation -a -d src/ai_data_reliability/service; then PYTHONPATH=src $(BIN)/lint-imports; fi
	@if test -d analytics/template/models; then $(BIN)/sqlfluff lint analytics/template/models analytics/template/tests; fi
	npm run lint
	npm run typecheck
	npm run build:web
	npm run format:check
	@if test -d web; then npm run unused; fi
	@if test -d src -o -d web; then SSL_CERT_FILE=.venv/lib/python3.12/site-packages/certifi/cacert.pem $(BIN)/semgrep scan --config p/python --config p/typescript --error $(wildcard src docker tests web); fi

mutation:
	@if test -f src/ai_data_reliability/investigator/sandbox.py; then $(BIN)/mutmut run --max-children 4; else echo 'Mutation targets become available in Phase 2'; fi

security:
	gitleaks dir . --no-banner

e2e:
	@if test -d web; then npm run e2e; else echo 'Browser flow becomes available in Phase 5'; fi

eval: sandbox-image
	PYTHONPATH=src $(PY) -m ai_data_reliability.evaluation $(EVAL_ARGS)

check: lint test mutation security e2e
