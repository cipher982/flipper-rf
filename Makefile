.PHONY: help venv deps start mock clean

# Defaults (override on command line: make start PORT=auto)
PORT ?= auto
HTTP_PORT ?= 8765
WS_PORT ?= 8766
WORK_DIR ?= /tmp/flipper_explore
FREQS ?=
CAPTURE_DURATION ?=

PY ?= python3

help:
	@echo "RF Observatory"
	@echo ""
	@echo "Targets:"
	@echo "  make start     Start the dashboard (primary)"
	@echo "  make mock      Start with synthetic signals (no Flipper)"
	@echo "  make deps      Install dependencies"
	@echo "  make clean     Remove .venv and temp files"
	@echo ""
	@echo "Common overrides:"
	@echo "  make start PORT=/dev/ttyACM0"
	@echo "  make start FREQS=433.92"
	@echo "  make start HTTP_PORT=8875 WS_PORT=8876"
	@echo "  make start CAPTURE_DURATION=0.4"

venv:
	uv venv

deps: venv
	. .venv/bin/activate && uv pip install websockets pyserial

define run_py
	. .venv/bin/activate && $(PY) rf_app.py \
		--port "$(PORT)" \
		--http-port "$(HTTP_PORT)" \
		--ws-port "$(WS_PORT)" \
		--work-dir "$(WORK_DIR)" \
		$(if $(FREQS),--freqs "$(FREQS)",) \
		$(if $(CAPTURE_DURATION),--capture-duration "$(CAPTURE_DURATION)",)
endef

start: deps
	$(call run_py)

mock: deps
	. .venv/bin/activate && $(PY) rf_app.py \
		--mock \
		--http-port "$(HTTP_PORT)" \
		--ws-port "$(WS_PORT)" \
		--work-dir "$(WORK_DIR)" \
		$(if $(FREQS),--freqs "$(FREQS)",) \
		$(if $(CAPTURE_DURATION),--capture-duration "$(CAPTURE_DURATION)",)

clean:
	rm -rf .venv "$(WORK_DIR)"
