VENV=.venv/bin/python

.venv:
	uv venv && uv pip install --python $(VENV) -e . pytest

test: .venv
	$(VENV) -m pytest -q

demo: .venv
	$(VENV) -m clreview.server --demo
