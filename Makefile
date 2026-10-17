.DEFAULT_GOAL := help
.PHONY: help sync install reinstall run serve open doctor test web-install web-build web-dev web-test e2e packaging live mac mac-run mac-test lint format typecheck check

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

sync: ## Install dependencies into .venv
	uv sync

install: ## Put `robin` on your PATH
	uv tool install .

reinstall: ## Reinstall the PATH tool after changes
	uv tool install --reinstall .

run: sync ## Start the service and open the dashboard
	uv run robin

serve: sync ## Run the service without opening the browser
	uv run robin serve --no-browser

open: ## Fresh sign-in link for the dashboard
	uv run robin open

doctor: ## Check runtimes, storage, port and supervisor
	uv run robin doctor

test: ## Python suite
	uv run pytest

web-install:
	cd web && npm install

web-build: web-install ## Rebuild the dashboard into src/robin/web/dist
	cd web && npm run build

web-dev: web-install ## Vite dev server for the dashboard
	cd web && npm run dev

web-test: web-install ## Web component tests
	cd web && npm test

e2e: ## Dashboard in a real browser
	ROBIN_E2E=1 uv run pytest tests/e2e

packaging: ## Build the wheel and run it from a clean venv
	ROBIN_PACKAGING=1 uv run pytest tests/packaging

live: ## Real Claude, Codex and Pi sessions (uses your logins)
	ROBIN_LIVE=1 uv run pytest tests/live

mac: ## Build the menu bar app into mac/build
	xcodebuild -project mac/Robin.xcodeproj -scheme Robin -configuration Debug -derivedDataPath mac/build build

mac-run: mac ## Build and launch the menu bar app
	open mac/build/Build/Products/Debug/Robin.app

mac-test: ## Menu bar app unit tests
	xcodebuild -project mac/Robin.xcodeproj -scheme Robin -derivedDataPath mac/build -destination 'platform=macOS' test

lint: ## Ruff lint and format check
	uv run ruff check .
	uv run ruff format --check src tests

format: ## Apply ruff fixes and formatting
	uv run ruff check --fix .
	uv run ruff format src tests

typecheck: ## mypy
	uv run mypy

check: lint typecheck test web-test ## Everything that runs without a browser or logins
