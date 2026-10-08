.PHONY: test lint dev seed deploy-check deploy

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

dev:
	.venv/bin/uvicorn cusdev.web.app:app --port 8210 --reload

seed:
	.venv/bin/python scripts/seed_demo.py

# --- Боевой деплой ---------------------------------------------------------
# Исключения — защита, а не украшение. data/ и samples/ — голоса и имена реальных
# людей; .env* — токены. В соседнем проекте rsync однажды унёс на сервер чужой
# .env.backup с SSH-ключом, поэтому маски широкие.
RSYNC_EXCLUDES = \
	--exclude '.git' \
	--exclude '.venv' \
	--exclude '.env' \
	--exclude '.env.*' \
	--exclude '*.env' \
	--exclude 'data/' \
	--exclude 'samples/' \
	--exclude '__pycache__' \
	--exclude '*.egg-info' \
	--exclude '.pytest_cache' \
	--exclude '.ruff_cache' \
	--exclude '*.json.key' \
	--exclude 'secrets/' \
	--exclude '.claude'

VPS ?= vps
VPS_PATH ?= /opt/cusdev

# Показать, что уедет на сервер, ничего не отправляя. Прогоняй перед deploy.
deploy-check:
	rsync -az --delete --dry-run --itemize-changes $(RSYNC_EXCLUDES) ./ $(VPS):$(VPS_PATH)/

deploy:
	rsync -az --delete $(RSYNC_EXCLUDES) ./ $(VPS):$(VPS_PATH)/
	ssh $(VPS) 'mkdir -p /opt/cusdev-data && cd $(VPS_PATH) \
		&& docker compose -f docker-compose.prod.yml up -d --build'
