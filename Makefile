.PHONY: help venv deps decode intel mock-decode mock-intel clean-venv clean-tmp

# Defaults (override on command line: make decode PORT=auto)
PORT ?= auto
HTTP_PORT ?= 8765
WS_PORT ?= 8766
WORK_DIR ?= /tmp/flipper_explore
FREQS ?=
CAPTURE_DURATION ?=

PY ?= python3

help:
	@echo "flipper-rf"
	@echo ""
	@echo "Targets:"
	@echo "  make venv          Create .venv via uv"
	@echo "  make deps          Install deps into .venv (uv pip)"
	@echo "  make decode        Run rf_decode.py (PORT=$(PORT))"
	@echo "  make intel         Run rf_intel.py (PORT=$(PORT))"
	@echo "  make mock-decode   Run rf_decode.py --mock"
	@echo "  make mock-intel    Run rf_intel.py --mock"
	@echo "  make clean-venv    Remove .venv"
	@echo "  make clean-tmp     Remove $(WORK_DIR) (CAREFUL)"
	@echo ""
	@echo "Common overrides:"
	@echo "  make decode PORT=auto"
	@echo "  make decode FREQS=433.92"
	@echo "  make decode HTTP_PORT=8875 WS_PORT=8876"
	@echo "  make decode WORK_DIR=/tmp/flipper_explore_decode"
	@echo "  make decode CAPTURE_DURATION=0.4"

venv:
	uv venv

deps: venv
	. .venv/bin/activate && uv pip install websockets pyserial

define run_py
	. .venv/bin/activate && $(PY) $(1) \
		--port "$(PORT)" \
		--http-port "$(HTTP_PORT)" \
		--ws-port "$(WS_PORT)" \
		--work-dir "$(WORK_DIR)" \
		$(if $(FREQS),--freqs "$(FREQS)",) \
		$(if $(CAPTURE_DURATION),--capture-duration "$(CAPTURE_DURATION)",)
endef

decode: deps
	$(call run_py,rf_decode.py)

intel: deps
	$(call run_py,rf_intel.py)

mock-decode: deps
	. .venv/bin/activate && $(PY) rf_decode.py \
		--mock \
		--http-port "$(HTTP_PORT)" \
		--ws-port "$(WS_PORT)" \
		--work-dir "$(WORK_DIR)" \
		$(if $(FREQS),--freqs "$(FREQS)",) \
		$(if $(CAPTURE_DURATION),--capture-duration "$(CAPTURE_DURATION)",)

mock-intel: deps
	. .venv/bin/activate && $(PY) rf_intel.py \
		--mock \
		--http-port "$(HTTP_PORT)" \
		--ws-port "$(WS_PORT)" \
		--work-dir "$(WORK_DIR)" \
		$(if $(FREQS),--freqs "$(FREQS)",) \
		$(if $(CAPTURE_DURATION),--capture-duration "$(CAPTURE_DURATION)",)

clean-venv:
	rm -rf .venv

clean-tmp:
	rm -rf "$(WORK_DIR)"

