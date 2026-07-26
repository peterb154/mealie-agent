.PHONY: test lint fmt fix lock requirements check

# One place for the test command, instead of it living in four test-module
# docstrings and whoever-last-ran-it's shell history.
test:
	uv run --group dev pytest -q

lint:
	uv run --group dev ruff check .

fmt:
	uv run --group dev ruff format --check .

fix:
	uv run --group dev ruff format .
	uv run --group dev ruff check . --fix

lock:
	uv lock

# The Docker image installs with pip, so requirements.txt has to stay a
# faithful export of the lock or the image drifts from what's tested. CI
# fails if this output differs from what's committed.
requirements:
	@{ \
	  echo "# GENERATED — do not edit by hand."; \
	  echo "# Source of truth is pyproject.toml + uv.lock. Regenerate with:"; \
	  echo "#   make requirements"; \
	  echo "# Kept because the Docker image installs with pip, so the lock and the"; \
	  echo "# image can't drift apart silently."; \
	  uv export --no-dev --no-hashes --no-emit-project | grep -v '^#'; \
	} > requirements.txt

check: lint test
