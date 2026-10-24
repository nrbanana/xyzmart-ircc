# Python 3.11 or newer. Override with: make local PYTHON=/path/to/python3.12
PYTHON ?= $(firstword $(shell command -v python3.13 python3.12 python3.11) python3)
VENV := .venv
PY := $(CURDIR)/$(VENV)/bin/python

.PHONY: setup local test

$(VENV)/bin/uvicorn: proto/requirements.txt proto/requirements-dev.txt
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || (echo "Needs Python 3.11+, found $$($(PYTHON) --version)"; exit 1)
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q -r proto/requirements-dev.txt
	@touch $@

setup: $(VENV)/bin/uvicorn  ## create the virtual environment
	@test -f proto/.env || (cp proto/.env.example proto/.env && echo "Created proto/.env: see the comments in it")

local: setup  ## run everything on this laptop (fresh sample data each time)
	rm -f proto/data/ircc.db
	@echo "\n  Demo screen: http://localhost:8080\n  Press Ctrl+C to stop.\n"
	@cd proto && trap 'kill 0' INT TERM; \
	$(PY) -m uvicorn --app-dir supplier_stub main:app --port 8082 --log-level warning & \
	$(PY) -m uvicorn --app-dir tool_service main:app --port 8081 --log-level warning & \
	$(PY) -m uvicorn --app-dir agent main:app --port 8080 --log-level warning & \
	wait

test: setup  ## unit tests and scripted-model agent runs, no network
	$(PY) -m pytest proto/tests -q

.PHONY: deploy destroy open cost-check

deploy: setup  ## create everything in Google Cloud and deploy (uses your gcloud login)
	scripts/deploy.sh

destroy:  ## remove everything from Google Cloud, then list anything left that could cost money
	scripts/destroy.sh

open:  ## open the cloud demo screen at http://localhost:8080 through your gcloud login
	gcloud run services proxy agent --region australia-southeast1 --port 8080

cost-check:  ## list anything in the project that could still cost money
	scripts/cost_check.sh
