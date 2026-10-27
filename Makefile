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

PORT ?= 8765

stop: ## stop the server listening on :8765 (PORT=... to override)
	@pid=$$(lsof -ti tcp:$(PORT) 2>/dev/null); \
	if [ -z "$$pid" ]; then echo "nothing listening on :$(PORT)"; exit 0; fi; \
	kill $$pid && echo "stopped pid $$pid on :$(PORT)"; \
	for i in 1 2 3 4 5 6 7 8 9 10; do sleep 0.2; lsof -ti tcp:$(PORT) >/dev/null 2>&1 || exit 0; done; \
	kill -9 $$(lsof -ti tcp:$(PORT)) 2>/dev/null && echo "force-killed :$(PORT)"; exit 0
