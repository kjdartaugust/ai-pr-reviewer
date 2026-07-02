"""AI PR reviewer — GitHub App.

Receives GitHub pull_request webhook events, authenticates as a GitHub App to
fetch the PR diff, sends it to OpenRouter for review, and posts the review back
as a PR comment. Authentication uses a JWT signed with the App's private key,
exchanged for a short-lived installation access token scoped to the repo.
"""

import datetime
import hashlib
import hmac
import logging
import os
import time

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-pr-reviewer")

APP_ID = os.environ.get("APP_ID")
# PEM private key. Render/env vars often store newlines as literal "\n".
PRIVATE_KEY = (os.environ.get("PRIVATE_KEY") or "").replace("\\n", "\n")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4")

GITHUB_API = "https://api.github.com"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"

# Events that should trigger a review.
REVIEW_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}

# Cap the diff we send to the model to keep token usage sane.
MAX_DIFF_CHARS = 60_000

SYSTEM_PROMPT = (
    "You are a senior software engineer performing a code review on a GitHub "
    "pull request. Review the unified diff below and report:\n"
    "1. Bugs or correctness issues\n"
    "2. Security vulnerabilities\n"
    "3. Concrete improvement suggestions\n\n"
    "Be specific and reference file names and line context where possible. "
    "Use concise Markdown with headings. If the diff looks clean, say so "
    "briefly instead of inventing problems."
)

app = FastAPI(title="AI PR Reviewer")

# Cache of installation_id -> (token, expiry_epoch) to avoid re-minting per event.
_installation_tokens: dict[int, tuple[str, float]] = {}


def verify_signature(body: bytes, signature: str | None) -> None:
    """Verify the GitHub App webhook HMAC signature if a secret is configured."""
    if not WEBHOOK_SECRET:
        return
    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature")
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")


def create_app_jwt() -> str:
    """Create a short-lived JWT signed with the App private key (RS256)."""
    now = int(time.time())
    payload = {
        "iat": now - 60,   # allow for clock drift
        "exp": now + 600,  # max 10 minutes
        "iss": APP_ID,
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")


async def get_installation_token(
    client: httpx.AsyncClient, installation_id: int
) -> str:
    """Return a cached or freshly minted installation access token."""
    cached = _installation_tokens.get(installation_id)
    if cached and cached[1] - 60 > time.time():
        return cached[0]

    resp = await client.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {create_app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["token"]
    expiry = datetime.datetime.fromisoformat(
        data["expires_at"].replace("Z", "+00:00")
    ).timestamp()
    _installation_tokens[installation_id] = (token, expiry)
    return token


async def fetch_diff(
    client: httpx.AsyncClient, token: str, repo: str, number: int
) -> str:
    """Fetch the unified diff for a pull request."""
    resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/pulls/{number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    resp.raise_for_status()
    return resp.text


async def review_diff(client: httpx.AsyncClient, diff: str) -> str:
    """Send the diff to OpenRouter and return the review text."""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n... [diff truncated] ..."

    resp = await client.post(
        OPENROUTER_API,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"```diff\n{diff}\n```"},
            ],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def post_comment(
    client: httpx.AsyncClient, token: str, repo: str, number: int, body: str
) -> None:
    """Post a comment on the pull request's conversation thread."""
    resp = await client.post(
        f"{GITHUB_API}/repos/{repo}/issues/{number}/comments",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": body},
    )
    resp.raise_for_status()


@app.get("/")
async def health() -> dict:
    return {"status": "ok", "service": "ai-pr-reviewer"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    body = await request.body()
    verify_signature(body, x_hub_signature_256)

    if x_github_event == "ping":
        return {"msg": "pong"}
    if x_github_event != "pull_request":
        return {"msg": f"ignored event: {x_github_event}"}

    payload = await request.json()
    action = payload.get("action")
    if action not in REVIEW_ACTIONS:
        return {"msg": f"ignored action: {action}"}

    if not APP_ID or not PRIVATE_KEY or not OPENROUTER_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="APP_ID, PRIVATE_KEY, and OPENROUTER_API_KEY must be set",
        )

    installation = payload.get("installation")
    if not installation:
        raise HTTPException(
            status_code=400, detail="No installation in payload; is this a GitHub App?"
        )
    installation_id = installation["id"]

    pr = payload["pull_request"]
    number = pr["number"]
    repo = payload["repository"]["full_name"]
    logger.info("Reviewing %s#%s (action=%s)", repo, number, action)

    async with httpx.AsyncClient() as client:
        try:
            token = await get_installation_token(client, installation_id)
        except httpx.HTTPStatusError as exc:
            logger.error("Failed to get installation token: %s", exc)
            raise HTTPException(status_code=502, detail="Auth with GitHub failed")

        try:
            diff = await fetch_diff(client, token, repo, number)
        except httpx.HTTPStatusError as exc:
            logger.error("Failed to fetch diff: %s", exc)
            raise HTTPException(status_code=502, detail="Failed to fetch PR diff")

        if not diff.strip():
            return {"msg": "empty diff, nothing to review"}

        try:
            review = await review_diff(client, diff)
        except httpx.HTTPStatusError as exc:
            logger.error("OpenRouter error: %s - %s", exc, exc.response.text)
            raise HTTPException(status_code=502, detail="Review generation failed")

        comment = f"## 🤖 AI Code Review\n\n{review}\n\n---\n*Reviewed by `{OPENROUTER_MODEL}` via OpenRouter.*"
        try:
            await post_comment(client, token, repo, number, comment)
        except httpx.HTTPStatusError as exc:
            logger.error("Failed to post comment: %s", exc)
            raise HTTPException(status_code=502, detail="Failed to post comment")

    return {"msg": "review posted", "repo": repo, "pr": number}
