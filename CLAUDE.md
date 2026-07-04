# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-file FastAPI service (`app.py`), packaged as a **GitHub App**, that reviews GitHub pull requests with an LLM. GitHub sends a `pull_request` webhook to `POST /webhook`; the service authenticates as the App, fetches the PR's unified diff, sends it to OpenRouter for review, and posts the result back as a PR comment via the Issues comments API.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # then fill in secrets

# Run (health check at http://localhost:8000/)
uvicorn app:app --reload --port 8000

# Expose to GitHub for webhook delivery
ngrok http 8000                                      # inspector at http://localhost:4040
```

There is no test suite, linter, or build step configured. Registration and Render deployment steps live in `README.md`.

## Request flow (app.py)

`webhook()` is the single entry point and orchestrates everything in order:
1. `verify_signature()` — HMAC-SHA256 check, **skipped entirely if `WEBHOOK_SECRET` is unset**.
2. Early-returns for `ping` events, non-`pull_request` events, and actions outside `REVIEW_ACTIONS` (`opened`, `reopened`, `synchronize`, `ready_for_review`).
3. `get_installation_token()` → `fetch_diff()` → `review_diff()` → `post_comment()`, all sharing one `httpx.AsyncClient`. Each wraps its call in try/except and re-raises as a `502`.

## GitHub App authentication

There are no personal access tokens. `create_app_jwt()` signs a short-lived RS256 JWT with `PRIVATE_KEY` (issuer `APP_ID`). `get_installation_token()` exchanges that JWT at `/app/installations/{id}/access_tokens` for an installation token scoped to the repo. The installation ID comes from `payload["installation"]["id"]` in the webhook. Tokens are cached in the module-level `_installation_tokens` dict keyed by installation ID and reused until ~60s before their `expires_at`.

## General review endpoints (no webhook)

Three on-demand endpoints reuse the OpenRouter logic and return JSON instead of
posting a comment: `POST /review/code` (raw snippet), `POST /review/commit`
(`{repo, sha}`, fetches the commit diff), `POST /review/repo` (`{repo, branch?}`,
concatenates source files up to `MAX_REPO_CHARS`). `review_diff()` now delegates
to a shared `call_openrouter()`; `SYSTEM_PROMPT` was generalized off "pull
request" so it serves both PR and commit diffs, with separate `CODE_SYSTEM_PROMPT`
and `REPO_SYSTEM_PROMPT`. `get_installation_token_for_repo()` looks up the App's
installation for a repo (via the app JWT) and returns `None` when the App isn't
installed, so commit/repo reviews fall back to unauthenticated access for public
repos.

## Environment variables

Loaded once at module import via `load_dotenv()` into module-level globals — changing `.env` requires a server restart. `APP_ID`, `PRIVATE_KEY`, and `OPENROUTER_API_KEY` are required (missing them yields a `500` at webhook time, not startup). `PRIVATE_KEY` is a PEM; literal `\n` sequences are converted to newlines at load time so it survives single-line env-var storage. `OPENROUTER_MODEL` defaults to `anthropic/claude-sonnet-4`; `WEBHOOK_SECRET` is optional but recommended.

## Constraints to keep in mind

- Diffs over `MAX_DIFF_CHARS` (60k) are truncated before being sent to the model.
- The GitHub diff is fetched with the `application/vnd.github.v3.diff` Accept header (returns raw text, not JSON).
- Comments post to `/issues/{number}/comments` — the PR conversation thread, not inline review comments.
