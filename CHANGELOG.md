# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CI quality gates: ruff/mypy advisory checks and pytest coverage floor enforced in pull-request CI.
- Dependabot configuration for automated dependency updates (Python pip + GitHub Actions).
- CodeQL static-analysis workflow for security scanning on push/PR to main.
- SHA-pinned GitHub Actions across all workflows (supply-chain hardening).
- Frontend CI workflow (`.github/workflows/frontend.yml`): TypeScript type-check, ESLint, and Next.js build triggered on pull requests touching `frontend/**`.
- Manual Docker build-check workflow (`.github/workflows/docker-build.yml`): compile-tests the legacy Gradio container image on demand without blocking per-PR CI.
- Weekly scheduled unit-test canary (cron) to catch silent breakage between pull requests.
- Fixed the previously-dead integration test job by adding a `workflow_dispatch` trigger so it can now be dispatched manually; it still does not run on pull requests (it needs a self-hosted runner with Ollama/Qdrant).

### Security

- Containment fix for CVE-style path-traversal vulnerability (#17) in `ParentStoreManager.load` / `ParentStoreManager.save`; added regression tests.

### Changed

- `project/Dockerfile`: upgraded base image from `python:3.13` to `python:3.14` to resolve three-way Python version drift between the Dockerfile, CI, and README.
