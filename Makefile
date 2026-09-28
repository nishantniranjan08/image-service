PYTHON ?= python3
VENV   ?= .venv
BIN     = $(VENV)/bin

.PHONY: help install up wait deploy demo test coverage test-integration logs down clean all

help:            ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-18s %s\n", $$1, $$2}'

install:         ## Create a virtualenv with dev/test dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -r requirements-dev.txt

up:              ## Start LocalStack in Docker
	docker compose up -d
	@$(MAKE) --no-print-directory wait

wait:
	@echo "Waiting for LocalStack..."
	@until curl -sf http://localhost:4566/_localstack/health >/dev/null; do sleep 2; done
	@echo "LocalStack is up."

deploy:          ## Create bucket, table, Lambdas and API Gateway in LocalStack
	$(BIN)/python scripts/deploy.py

demo:            ## Run every endpoint end-to-end against the deployed API
	$(BIN)/python scripts/demo.py

test:            ## Run unit tests (AWS mocked with moto; no Docker needed)
	$(BIN)/pytest

coverage:        ## Run unit tests with a coverage report
	$(BIN)/pytest --cov --cov-report=term-missing

test-integration: ## Run end-to-end tests against the LocalStack deployment
	$(BIN)/pytest -m integration

logs:            ## Tail LocalStack logs (includes Lambda output)
	docker compose logs -f localstack

down:            ## Stop LocalStack
	docker compose down

clean: down      ## Stop LocalStack and remove local artifacts
	rm -rf $(VENV) .pytest_cache .coverage htmlcov .api_url
	find . -name __pycache__ -prune -exec rm -rf {} +

all: install up deploy demo  ## Everything, from a fresh clone to a working demo
