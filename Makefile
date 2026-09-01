.PHONY: help generate generate-enums generate-rest generate-ws stamp-const clean-wire test lint

# The daemon repository the wire bindings are generated from. Every target
# below reads its committed contract assets — never a running daemon.
OPENCCU_LOOM_REPO ?= ../openccu-loom
WIRE := openccu_loom_client/wire

help: ## show this help
	@awk -F':.*?## ' '/^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

generate: generate-enums generate-rest generate-ws stamp-const ## regenerate every module under openccu_loom_client/wire/

stamp-const: ## stamp wire/const.py with the daemon's schema digest + api_version
	python3 script/gen/stamp_const.py \
		--openccu-loom-repo $(OPENCCU_LOOM_REPO) \
		--const-py $(WIRE)/const.py

generate-enums: ## regenerate wire/enums.py from $(OPENCCU_LOOM_REPO)/assets/schemas/enums.json
	python3 script/gen/gen_enums.py \
		--enums-json $(OPENCCU_LOOM_REPO)/assets/schemas/enums.json \
		--out-py $(WIRE)/enums.py
	python3 script/gen/tolerant_enums.py --py $(WIRE)/enums.py

# The tolerant-enums step runs AFTER the generator in both recipes, never as a
# separate target: a bare `make generate-rest` would otherwise hand back an
# enum that raises on the first value a newer daemon adds. The step is
# idempotent and content-only, so it keeps the deterministic output the
# comment below insists on.
#
# --disable-timestamp is REQUIRED for deterministic output: without it
# datamodel-codegen stamps the current wall-clock time into the rest.py header,
# so every regeneration diffs against the last one even when the daemon API is
# byte-for-byte identical. That spurious diff defeats the "skip when the API is
# unchanged" guard in .github/workflows/regenerate-on-daemon-release.yml.
generate-rest: ## regenerate wire/rest.py via datamodel-codegen
	@command -v datamodel-codegen >/dev/null 2>&1 || { \
		echo "datamodel-codegen not on PATH — install via 'pip install -e .[dev]'"; exit 1; }
	datamodel-codegen \
		--input $(OPENCCU_LOOM_REPO)/assets/openapi.yaml \
		--input-file-type openapi \
		--output $(WIRE)/rest.py \
		--output-model-type pydantic_v2.BaseModel \
		--target-python-version 3.11 \
		--use-standard-collections \
		--use-double-quotes \
		--field-constraints \
		--disable-timestamp \
		--formatters ruff-format ruff-check
	python3 script/gen/tolerant_enums.py --py $(WIRE)/rest.py

generate-ws: ## regenerate wire/ws.py (envelope + push-payload re-exports from rest.py)
	python3 script/gen/gen_ws.py \
		--wsapi-json $(OPENCCU_LOOM_REPO)/assets/wsapi.json \
		--rest-py $(WIRE)/rest.py \
		--out-py $(WIRE)/ws.py

generate-consumed-operations: ## refresh spec/consumed_operations.json from the façade call sites
	python3 script/gen/consumed_operations.py

check-consumed-operations: ## fail when spec/consumed_operations.json is stale
	python3 script/gen/consumed_operations.py --check

clean-wire: ## remove the generated modules (const.py is stamped, not generated)
	rm -f $(WIRE)/enums.py $(WIRE)/rest.py $(WIRE)/ws.py

test: ## run pytest
	pytest -q

lint: ## ruff + mypy + pylint, as CI runs them
	ruff check .
	ruff format --check .
	mypy openccu_loom_client
	pylint openccu_loom_client
