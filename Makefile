.PHONY: help install dev runner worker docker-up docker-down docker-build \
        migrate migrate-status migrate-dry-run \
        lint format clean test test-unit test-report test-cov test-full \
        ngrok logs-runner logs-worker ecr-push

help:
	@echo "Invorto AI - Voice Bot Platform"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@echo "Setup:"
	@echo "  install           Install Python dependencies"
	@echo "  dev               Install dev dependencies and setup venv"
	@echo "  migrate           Apply all pending DB migrations (via invorto-db submodule)"
	@echo "  migrate-status    Show applied / pending migrations"
	@echo "  migrate-dry-run   Print migration SQL without applying"
	@echo ""
	@echo "Development:"
	@echo "  runner         Start the bot runner (port 7860)"
	@echo "  worker         Start the bot worker (port 8765)"
	@echo "  runner-reload  Start runner with hot reload"
	@echo "  worker-reload  Start worker with hot reload"
	@echo "  ngrok          Start ngrok tunnel to port 7860"
	@echo ""
	@echo "Docker:"
	@echo "  docker-build   Build Docker images"
	@echo "  docker-up      Start services with docker-compose"
	@echo "  docker-down    Stop docker-compose services"
	@echo "  docker-logs    View docker-compose logs"
	@echo ""
	@echo "Quality:"
	@echo "  lint           Run linter (ruff)"
	@echo "  format         Format code (ruff)"
	@echo "  test           Run all tests, excluding slow load tests"
	@echo "  test-unit      Run unit tests only (no Docker needed)"
	@echo "  test-load      Run slow load tests only (@pytest.mark.slow)"
	@echo "  test-report    Run all tests + open HTML report"
	@echo "  test-cov       Run all tests + open coverage report"
	@echo "  test-full      Run all tests + HTML report + coverage report"
	@echo ""
	@echo "Utilities:"
	@echo "  clean          Clean up cache files"
	@echo "  health         Check health of running services"
	@echo "  workers        List worker status"

install:
	pip install --upgrade pip
	pip install -r requirements.runner.txt -r requirements.worker.txt

install-runner:
	pip install --upgrade pip
	pip install -r requirements.runner.txt

install-worker:
	pip install --upgrade pip
	pip install -r requirements.worker.txt

dev:
	python3.11 -m venv .venv
	. venv/bin/activate && pip install --upgrade pip
	. venv/bin/activate && pip install -r requirements.runner.txt -r requirements.worker.txt
	@echo ""
	@echo "Virtual environment created. Activate with:"
	@echo "  source venv/bin/activate"

migrate:
	cd db && supabase db push --project-ref $(SUPABASE_PROJECT_REF)

migrate-status:
	python db/migrate.py --status

migrate-dry-run:
	python db/migrate.py --dry-run

runner:
	ENVIRONMENT=local python app/run_runner.py

worker:
	ENVIRONMENT=local python app/run_worker.py

runner-reload:
	ENVIRONMENT=local uvicorn app.main:app --host 0.0.0.0 --port 7860 --reload

worker-reload:
	ENVIRONMENT=local uvicorn app.worker.main:app --host 0.0.0.0 --port 8765 --reload

ngrok:
	ngrok start --config ngrok.yml --all 

docker-build:
	docker-compose build

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

docker-rebuild:
	docker-compose down
	docker-compose build
	docker-compose up -d

ecr-push:
	@bash ./scripts/push_to_ecr.sh --help >/dev/null 2>&1 || (echo "Missing scripts/push_to_ecr.sh" && exit 1)
	@echo "Usage example:"
	@echo "  make ecr-push AWS_REGION=ap-south-1 AWS_ACCOUNT_ID=123456789012 TAG=latest"
	@echo ""
	@test -n "$(AWS_REGION)" || (echo "AWS_REGION is required" && exit 1)
	@test -n "$(AWS_ACCOUNT_ID)" || (echo "AWS_ACCOUNT_ID is required" && exit 1)
	@bash ./scripts/push_to_ecr.sh \
	  --region "$(AWS_REGION)" \
	  --account-id "$(AWS_ACCOUNT_ID)" \
	  --runner-repo "$(or $(RUNNER_REPO),invorto-ai-runner)" \
	  --worker-repo "$(or $(WORKER_REPO),invorto-ai-worker)" \
	  --tag "$(or $(TAG),latest)"

test:
	.venv/bin/pytest tests/ -q -m "not slow"

test-unit:
	.venv/bin/pytest tests/unit/ -q -m "not slow"

test-load:
	.venv/bin/pytest tests/unit/ -m slow -v

test-report:
	@mkdir -p reports
	.venv/bin/pytest tests/ -q \
		--html=reports/test-report.html \
		--self-contained-html
	@echo ""
	@echo "Report: reports/test-report.html"
	@open reports/test-report.html 2>/dev/null || xdg-open reports/test-report.html 2>/dev/null || true

test-cov:
	@mkdir -p reports
	.venv/bin/pytest tests/ -q \
		--cov=app \
		--cov-report=term-missing \
		--cov-report=html:reports/coverage
	@echo ""
	@echo "Coverage: reports/coverage/index.html"
	@open reports/coverage/index.html 2>/dev/null || xdg-open reports/coverage/index.html 2>/dev/null || true

test-full:
	@mkdir -p reports
	.venv/bin/pytest tests/ -q \
		--html=reports/test-report.html \
		--self-contained-html \
		--cov=app \
		--cov-report=term-missing \
		--cov-report=html:reports/coverage \
		--junitxml=reports/junit.xml
	@echo ""
	@echo "Reports generated:"
	@echo "  HTML test report : reports/test-report.html"
	@echo "  Coverage report  : reports/coverage/index.html"
	@echo "  JUnit XML        : reports/junit.xml"
	@open reports/test-report.html 2>/dev/null || xdg-open reports/test-report.html 2>/dev/null || true

lint:
	@which ruff > /dev/null || pip install ruff
	ruff check app/

format:
	@which ruff > /dev/null || pip install ruff
	ruff format app/
	ruff check --fix app/

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name ".DS_Store" -delete 2>/dev/null || true
	@echo "Cleaned up cache files"

health:
	@echo "Runner health:"
	@curl -s http://localhost:7860/health | python -m json.tool 2>/dev/null || echo "  Runner not running"
	@echo ""
	@echo "Worker health:"
	@curl -s http://localhost:8765/health | python -m json.tool 2>/dev/null || echo "  Worker not running"

workers:
	@curl -s http://localhost:7860/workers | python -m json.tool 2>/dev/null || echo "Runner not running"

prefetch-models:
	@echo "Pre-fetching AI models..."
	python scripts/prefetch_smart_turn_model.py
