.DEFAULT_GOAL := help
COMPOSE       := docker compose
BOLD  := \033[1m
RESET := \033[0m
CYAN  := \033[36m
GREEN := \033[32m
RED   := \033[31m

.PHONY: help up down down-v build logs ps health demo shell-pg \
        db-journal db-balances db-audit db-settlements db-recon topics

help: ## Show this help
	@echo ""
	@echo "$(BOLD)Permissioned DeFi Compliance Engine$(RESET)"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { \
		printf "  $(CYAN)%-22s$(RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

up: ## Build and start all services
	$(COMPOSE) up --build -d
	@echo "$(GREEN)Stack started — run 'make health' to verify.$(RESET)"

down: ## Stop containers (keep volumes)
	$(COMPOSE) down

down-v: ## Stop containers AND remove volumes (full reset)
	$(COMPOSE) down -v

build: ## Rebuild all images without starting
	$(COMPOSE) build --no-cache

logs: ## Follow all service logs
	$(COMPOSE) logs -f

ps: ## Show container status
	$(COMPOSE) ps

health: ## Check gateway health (aggregated)
	@curl -sf http://localhost:8000/health | python3 -m json.tool || \
	  echo "$(RED)Gateway not reachable — run 'make up' first$(RESET)"

demo: ## Run the end-to-end demo
	GATEWAY_URL=http://localhost:8000 API_KEY=admin-key-demo-001 \
	  python3 scripts/demo.py

shell-pg: ## Open psql shell
	$(COMPOSE) exec postgres psql -U defi_user -d defi_db

db-journal: ## Show recent journal entries
	$(COMPOSE) exec postgres psql -U defi_user -d defi_db -c \
	  "SELECT journal_id, coa_code, currency, debit, credit, entry_type, created_at \
	   FROM journal_entries ORDER BY created_at DESC LIMIT 20;"

db-balances: ## Show all account balances (derived from ledger)
	$(COMPOSE) exec postgres psql -U defi_user -d defi_db -c \
	  "SELECT * FROM account_balances;"

db-audit: ## Show recent audit events
	$(COMPOSE) exec postgres psql -U defi_user -d defi_db -c \
	  "SELECT trace_id, actor, actor_role, action, resource_type, created_at \
	   FROM audit_events ORDER BY created_at DESC LIMIT 20;"

db-settlements: ## Show settlements with current status
	$(COMPOSE) exec postgres psql -U defi_user -d defi_db -c \
	  "SELECT s.id, s.amount, s.currency, scs.status, scs.tx_hash, scs.created_at \
	   FROM settlements s \
	   LEFT JOIN settlement_current_status scs ON scs.settlement_id = s.id \
	   ORDER BY s.created_at DESC LIMIT 20;"

db-recon: ## Show reconciliation run history
	$(COMPOSE) exec postgres psql -U defi_user -d defi_db -c \
	  "SELECT id, run_type, status, total_checked, mismatches, started_at, completed_at \
	   FROM reconciliation_runs ORDER BY started_at DESC LIMIT 10;"

topics: ## List Kafka topics
	$(COMPOSE) exec kafka kafka-topics --bootstrap-server localhost:9092 --list

db-outbox: ## Show outbox event delivery status
	$(COMPOSE) exec postgres psql -U defi_user -d defi_db -c \
	  "SELECT * FROM outbox_delivery_status ORDER BY created_at DESC LIMIT 20;"

db-dlq: ## Show dead letter queue
	$(COMPOSE) exec postgres psql -U defi_user -d defi_db -c \
	  "SELECT id, source_table, event_type, error_message, retry_count, created_at \
	   FROM dead_letter_queue WHERE resolved_at IS NULL ORDER BY created_at DESC;"

db-rbac: ## Show RBAC configuration
	$(COMPOSE) exec postgres psql -U defi_user -d defi_db -c \
	  "SELECT ar.actor, r.role_name \
	   FROM rbac_actor_roles ar \
	   JOIN rbac_roles r ON r.id = ar.role_id \
	   ORDER BY ar.actor;"

db-immutable-test: ## Verify immutability (should raise exception)
	@echo "Testing UPDATE block on journal_entries..."
	@$(COMPOSE) exec postgres psql -U defi_user -d defi_db -c \
	  "UPDATE journal_entries SET debit = 0 WHERE id = (SELECT id FROM journal_entries LIMIT 1);" \
	  2>&1 || echo "$(GREEN)Immutability enforced — UPDATE blocked as expected.$(RESET)"
