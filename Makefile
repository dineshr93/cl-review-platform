VENV=.venv/bin/python

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?##' Makefile | awk -F':.*?##' '{printf "%-8s %s\n", $$1, $$2}'

.venv:
	uv venv && uv pip install --python $(VENV) -e . pytest

test: .venv ## run pytest
	$(VENV) -m pytest -q

demo: .venv ## seed mock PL/SL data and serve UI on :8765
	$(VENV) -m clreview.server --demo
