# caldav-forwarder — build, test, and deploy the SAM application.
# First deploy: `make deploy-guided` (prompts for parameters, saves them to samconfig.toml).
# Thereafter:   `make deploy`.
#
# samconfig.toml holds your real deploy parameters (feed URL, emails) and is gitignored.
# Copy samconfig.example.toml to samconfig.toml and fill it in before the first deploy.

STACK ?= caldav-forwarder

.DEFAULT_GOAL := help
.PHONY: help install lint fmt test check validate build deploy deploy-guided sync logs delete clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS = ":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Sync runtime + dev dependencies into the uv venv
	uv sync

lint: ## Lint, format-check, and type-check
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check

fmt: ## Auto-format and apply lint fixes
	uv run ruff format .
	uv run ruff check --fix .

test: ## Run the test suite
	uv run pytest -q

check: lint test validate ## Run all pre-deploy checks

validate: ## Validate and lint the SAM template
	sam validate --lint

build: ## Build the Lambda artifacts (python3.14)
	sam build

deploy: build ## Deploy using parameters in samconfig.toml
	sam deploy

deploy-guided: build ## First-time interactive deploy (writes samconfig.toml)
	sam deploy --guided

sync: ## Hot-reload changes to the deployed stack (Ctrl-C to stop)
	sam sync --stack-name $(STACK) --watch

logs: ## Tail CloudWatch logs for the stack
	sam logs --stack-name $(STACK) --tail

delete: ## Delete the deployed stack
	sam delete --stack-name $(STACK)

clean: ## Remove local build artifacts
	rm -rf .aws-sam
