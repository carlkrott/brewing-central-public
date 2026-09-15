.PHONY: test test-unit test-browser test-runtime verify-offline

test:
	./scripts/run-tests.sh all

test-unit:
	./scripts/run-tests.sh unit

test-browser:
	./scripts/run-tests.sh browser

test-runtime:
	./scripts/run-tests.sh runtime

verify-offline:
	./scripts/run-tests.sh verify-offline
