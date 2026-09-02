# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-02

### Added
- SQLite action journal with idempotency-key protection
- Recovery from `unknown` outcomes via read-only verification
- Explicit retry only after `verified_absent`
- LangGraph workflow with SQLite checkpoints and restart recovery
- GitHub issue adapter with deterministic markers and read-only inspection
- Human approval gate for retry (`request_retry_approval`, `approve_retry`, `reject_retry`)
- Verification outcomes distinguishing found, verified_absent, unavailable, ambiguous
- Legacy SQLite schema migration
- Recovery of running actions after process restart
- Pull-request exclusion from GitHub issue lookup

[Unreleased]: https://github.com/Ayush-yadav11/agent-recovery-runtime/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Ayush-yadav11/agent-recovery-runtime/releases/tag/v0.1.0
