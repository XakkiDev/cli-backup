PYTHON ?= python3
VENV := .venv

.PHONY: venv install test lint test-integration
venv:
	$(PYTHON) -m venv $(VENV)

install:
	$(VENV)/bin/python -m pip install -e ".[dev]"

test:
	$(VENV)/bin/python -m unittest discover -s tests -v

lint:
	$(VENV)/bin/ruff check --select E9,F63,F7,F82 src tests
	$(VENV)/bin/python -m compileall -q src tests

test-integration:
	$(VENV)/bin/python -m unittest discover -s tests/integration -v
