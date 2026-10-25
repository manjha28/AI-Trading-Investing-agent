.PHONY: lint

lint:
	cd backend && ruff check --select F821 .
	cd backend && ruff check --select F811,F841 --exit-zero .
