# Contributing

Open an issue with a reproducible case before a broad change. Keep patches focused, add tests for behavior changes, and run `make lint`, `make test`, and `make e2e` before proposing a pull request. Use `make lint test security e2e` for the runner-compatible gate; a separate local mutation pass exercises the guard and scoring rules. Do not commit credentials, runtime data, or warehouse files.
