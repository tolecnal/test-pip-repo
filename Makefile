# Convenience wrappers around ./pipcheck. Everything here is optional.
#
#   make bootstrap            install build/twine/devpi-client into .venvs/tooling
#   make doctor               is the server reachable and sanely configured?
#   make test                 patch bump -> build -> upload -> full suite
#   make test BUMP=minor      same, with a minor bump
#   make verify               re-run the suite against the last build
#   make compare              diff the two most recent runs (the upgrade check)
#   make lint                 ruff + isort --check-only + pyright
#   make format               apply isort
#   make release VERSION=2.0.0
#   make clean
#
# Upgrade check:  make test  ->  upgrade the server  ->  make test  ->  make compare

BUMP ?= patch
PIPCHECK ?= ./pipcheck
PYTHON ?= python3
LINT := .venvs/lint
SOURCES := harness pkg

.PHONY: help bootstrap doctor show test verify quick compare reports release build upload remove clean list lint format

help:
	@$(PIPCHECK) --help

bootstrap:
	$(PIPCHECK) bootstrap

doctor:
	$(PIPCHECK) doctor

show:
	$(PIPCHECK) show

list:
	$(PIPCHECK) list

# The main command: new version, publish it, prove it came back correctly.
# Each run saves a report into reports/ for `make compare`.
test:
	$(PIPCHECK) cycle --bump $(BUMP)

# Faster feedback loop: skip the slow mirror/pytest checks.
quick:
	$(PIPCHECK) cycle --bump $(BUMP) --skip package_tests,mirror_cache,install_older_pin

verify:
	$(PIPCHECK) verify

# Did the upgrade change anything? Diffs the two most recent runs.
compare:
	$(PIPCHECK) compare

reports:
	$(PIPCHECK) compare --list

release:
	@test -n "$(VERSION)" || (echo "usage: make release VERSION=X.Y.Z" && false)
	$(PIPCHECK) cycle --set-version $(VERSION)

build:
	$(PIPCHECK) build

upload:
	$(PIPCHECK) upload

remove:
	$(PIPCHECK) remove $(SPEC)

clean:
	$(PIPCHECK) clean

# --- code checks -------------------------------------------------------------
# The lint venv is rebuilt whenever requirements-dev.txt changes.
$(LINT)/bin/pyright: requirements-dev.txt
	$(PYTHON) -m venv $(LINT)
	$(LINT)/bin/pip install -q --upgrade pip
	$(LINT)/bin/pip install -q -r requirements-dev.txt
	@touch $@

lint: $(LINT)/bin/pyright
	$(LINT)/bin/ruff check $(SOURCES)
	$(LINT)/bin/isort --check-only --diff $(SOURCES)
	$(LINT)/bin/pyright

format: $(LINT)/bin/pyright
	$(LINT)/bin/ruff check --fix $(SOURCES)
	$(LINT)/bin/isort $(SOURCES)
