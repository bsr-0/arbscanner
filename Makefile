# arbscanner - Cross-platform prediction market arbitrage scanner
# Common developer tasks. Run `make help` for a list of targets.

.DEFAULT_GOAL := help

.PHONY: help install install-node test test-fast lint format scan match serve \
        doctor clean clean-data docker-build docker-run docker-logs

help: ## Show this help message
	@echo "arbscanner - available targets:"
	@echo ""
	@echo "  help          Show this help message"
	@echo "  install       Install Python deps and editable package (dev group included)"
	@echo "  install-node  Install the pmxtjs Node sidecar globally"
	@echo "  test          Run the full test suite (verbose)"
	@echo "  test-fast     Run tests, failing fast and prioritizing last failures"
	@echo "  lint          Lint src/ and tests/ with ruff"
	@echo "  format        Format src/ and tests/ with ruff"
	@echo "  doctor        Check environment: Node/pmxtjs, data dir, credentials, connectivity"
	@echo "  scan          Run the live arb scanner dashboard"
	@echo "  match         Run the market matching pipeline"
	@echo "  serve         Start the FastAPI web server with auto-reload"
	@echo "  clean         Remove Python build/cache artifacts"
	@echo "  clean-data    Remove the scanner database and data/ directory (prompts)"
	@echo "  docker-build  Build the arbscanner Docker image"
	@echo "  docker-run    Start services via docker compose (detached)"
	@echo "  docker-logs   Tail logs from the scanner service"

install: ## Install Python deps and editable package (dev group included)
	uv sync

install-node: ## Install the pmxtjs Node sidecar globally
	npm install -g pmxtjs

test: ## Run the full test suite (verbose)
	uv run pytest tests/ -v

test-fast: ## Run tests, failing fast and prioritizing last failures
	uv run pytest tests/ -x --ff

lint: ## Lint src/ and tests/ with ruff
	uv run ruff check src/ tests/

format: ## Format src/ and tests/ with ruff
	uv run ruff format src/ tests/

doctor: ## Check environment: Node/pmxtjs, data dir, credentials, connectivity
	uv run arbscanner doctor

scan: ## Run the live arb scanner dashboard
	uv run arbscanner scan

match: ## Run the market matching pipeline
	uv run arbscanner match

serve: ## Start the FastAPI web server with auto-reload
	uv run arbscanner serve --reload

clean: ## Remove Python build/cache artifacts
	rm -rf .pytest_cache .ruff_cache dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +

clean-data: ## Remove the scanner database and data/ directory (prompts)
	@read -p "This will delete arbscanner.db and data/. Continue? [y/N] " ans; \
	if [ "$$ans" = "y" ] || [ "$$ans" = "Y" ]; then \
		rm -rf arbscanner.db data/; \
		echo "Removed arbscanner.db and data/"; \
	else \
		echo "Aborted."; \
	fi

docker-build: ## Build the arbscanner Docker image
	docker build -t arbscanner:latest .

docker-run: ## Start services via docker compose (detached)
	docker compose up -d

docker-logs: ## Tail logs from the scanner service
	docker compose logs -f scanner
