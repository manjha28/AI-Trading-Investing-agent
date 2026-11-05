.PHONY: lint dev-codespaces

SHELL := /bin/bash

lint:
	cd backend && ruff check --select F821 .
	cd backend && ruff check --select F811,F841 --exit-zero .

dev-codespaces:
	@set -m; \
	(cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000) & backend_pid=$$!; \
	(cd frontend && npm run dev) & frontend_pid=$$!; \
	trap 'kill $$backend_pid $$frontend_pid 2>/dev/null || true' EXIT INT TERM; \
	wait -n $$backend_pid $$frontend_pid
