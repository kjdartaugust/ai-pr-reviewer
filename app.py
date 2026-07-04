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
import pathlib
import time

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

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

# Cap the content we send to the model to keep token usage sane.
MAX_DIFF_CHARS = 60_000       # per-diff budget (PR + commit reviews)
MAX_CODE_CHARS = 60_000       # per-snippet budget (paste-code reviews)
MAX_REPO_CHARS = 60_000       # total budget for a full-repo scan
MAX_FILE_CHARS = 12_000       # per-file cap within a repo scan

# Source-file extensions worth including in a full-repo scan.
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".kt", ".scala", ".m",
    ".sh", ".sql", ".vue", ".svelte", ".lua", ".dart", ".r", ".ex", ".exs",
}

# Shared instruction on what to report, appended to each review's system prompt.
_REVIEW_RUBRIC = (
    "Begin with a one-line **Verdict:** stating the overall outcome and a count "
    "of issues by severity (e.g. `Verdict: 1 Critical, 2 High, 1 Low — needs "
    "changes`). Then report findings grouped as:\n"
    "1. Bugs or correctness issues\n"
    "2. Security vulnerabilities\n"
    "3. Improvement suggestions\n\n"
    "Tag every finding with a severity label — **[Critical]**, **[High]**, "
    "**[Medium]**, or **[Low]** — and order findings from most to least severe. "
    "Prioritize issues that genuinely matter (correctness, security, data loss); "
    "do not pad the review with trivial style nitpicks. Be specific and reference "
    "file names and line context where possible. Use concise Markdown with "
    "headings. If the code is clean, say so in the Verdict and keep the rest "
    "brief instead of inventing problems."
)

# For unified diffs (pull requests and single commits).
SYSTEM_PROMPT = (
    "You are a senior software engineer performing a code review. Review the "
    "unified diff below. " + _REVIEW_RUBRIC
)

# For a raw code snippet or single file pasted by the user.
CODE_SYSTEM_PROMPT = (
    "You are a senior software engineer reviewing a snippet of code. Review "
    "the code below. " + _REVIEW_RUBRIC
)

# For a concatenated snapshot of many files across a repository.
REPO_SYSTEM_PROMPT = (
    "You are a senior software engineer performing a review of an entire "
    "repository. Below is a snapshot of its source files, each prefixed with "
    "its path. Give an overall assessment plus the most important findings. "
    + _REVIEW_RUBRIC
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


async def get_installation_token_for_repo(
    client: httpx.AsyncClient, repo: str
) -> str | None:
    """Return an installation token for `repo` if the App is installed there.

    Returns None (rather than raising) when the App has no installation on the
    repo or the App credentials are unset — callers fall back to unauthenticated
    requests, which work for public repositories.
    """
    if not APP_ID or not PRIVATE_KEY:
        return None
    try:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/installation",
            headers={
                "Authorization": f"Bearer {create_app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        return None
    return await get_installation_token(client, resp.json()["id"])


async def fetch_commit_diff(
    client: httpx.AsyncClient, token: str | None, repo: str, sha: str
) -> str:
    """Fetch the unified diff for a single commit."""
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/commits/{sha}", headers=headers
    )
    resp.raise_for_status()
    return resp.text


async def fetch_file_content(
    client: httpx.AsyncClient, token: str | None, repo: str, ref: str, path: str
) -> str | None:
    """Fetch a single file's raw text, or None if it can't be read as text."""
    headers = {
        "Accept": "application/vnd.github.v3.raw",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        params={"ref": ref},
        headers=headers,
    )
    if resp.status_code != 200:
        return None
    return resp.text


async def fetch_repo_snapshot(
    client: httpx.AsyncClient, token: str | None, repo: str, branch: str | None
) -> tuple[str, list[str], str]:
    """Concatenate a repo's source files into one snapshot (within budget).

    Returns (snapshot_text, included_paths, branch). Files are added in tree
    order until MAX_REPO_CHARS is reached; each file is capped at MAX_FILE_CHARS.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if not branch:
        info = await client.get(f"{GITHUB_API}/repos/{repo}", headers=headers)
        info.raise_for_status()
        branch = info.json()["default_branch"]

    tree_resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
        headers=headers,
    )
    tree_resp.raise_for_status()
    blobs = [
        item
        for item in tree_resp.json().get("tree", [])
        if item.get("type") == "blob"
        and os.path.splitext(item["path"])[1].lower() in CODE_EXTENSIONS
    ]

    parts: list[str] = []
    included: list[str] = []
    total = 0
    for item in blobs:
        if total >= MAX_REPO_CHARS:
            break
        content = await fetch_file_content(client, token, repo, branch, item["path"])
        if content is None:
            continue
        block = f"--- {item['path']} ---\n{content[:MAX_FILE_CHARS]}\n"
        parts.append(block)
        included.append(item["path"])
        total += len(block)

    return "\n".join(parts), included, branch


async def call_openrouter(
    client: httpx.AsyncClient, system_prompt: str, user_content: str
) -> str:
    """Send a system+user message pair to OpenRouter and return the reply."""
    resp = await client.post(
        OPENROUTER_API,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def review_diff(client: httpx.AsyncClient, diff: str) -> str:
    """Send a unified diff to OpenRouter and return the review text."""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n... [diff truncated] ..."
    return await call_openrouter(client, SYSTEM_PROMPT, f"```diff\n{diff}\n```")


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


_UI_PATH = pathlib.Path(__file__).parent / "static" / "index.html"


@app.get("/app", response_class=HTMLResponse)
async def ui() -> str:
    """Serve the paste-code web UI."""
    try:
        return _UI_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="UI not found")


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


# --- General code-review endpoints (no webhook / PR required) ----------------


def _require_openrouter() -> None:
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY must be set")


class CodeReviewRequest(BaseModel):
    code: str
    language: str | None = None
    filename: str | None = None


class CommitReviewRequest(BaseModel):
    repo: str  # "owner/name"
    sha: str


class RepoScanRequest(BaseModel):
    repo: str  # "owner/name"
    branch: str | None = None  # defaults to the repo's default branch


@app.post("/review/code")
async def review_code(req: CodeReviewRequest) -> dict:
    """Review a raw code snippet pasted in the request body."""
    _require_openrouter()
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="No code provided")

    code = req.code
    if len(code) > MAX_CODE_CHARS:
        code = code[:MAX_CODE_CHARS] + "\n\n... [code truncated] ..."

    label = req.filename or "snippet"
    fence = req.language or ""
    user_content = f"File: {label}\n\n```{fence}\n{code}\n```"

    async with httpx.AsyncClient() as client:
        try:
            review = await call_openrouter(client, CODE_SYSTEM_PROMPT, user_content)
        except httpx.HTTPStatusError as exc:
            logger.error("OpenRouter error: %s - %s", exc, exc.response.text)
            raise HTTPException(status_code=502, detail="Review generation failed")

    return {"review": review, "model": OPENROUTER_MODEL}


@app.post("/review/commit")
async def review_commit(req: CommitReviewRequest) -> dict:
    """Review the changes introduced by a single commit."""
    _require_openrouter()

    async with httpx.AsyncClient() as client:
        token = await get_installation_token_for_repo(client, req.repo)
        try:
            diff = await fetch_commit_diff(client, token, req.repo, req.sha)
        except httpx.HTTPStatusError as exc:
            status = 404 if exc.response.status_code == 404 else 502
            detail = (
                "Commit or repo not found (or the App isn't installed on a "
                "private repo)"
                if status == 404
                else "Failed to fetch commit diff"
            )
            logger.error("Failed to fetch commit diff: %s", exc)
            raise HTTPException(status_code=status, detail=detail)

        if not diff.strip():
            return {"msg": "empty diff, nothing to review"}

        try:
            review = await review_diff(client, diff)
        except httpx.HTTPStatusError as exc:
            logger.error("OpenRouter error: %s - %s", exc, exc.response.text)
            raise HTTPException(status_code=502, detail="Review generation failed")

    return {"review": review, "repo": req.repo, "sha": req.sha, "model": OPENROUTER_MODEL}


@app.post("/review/repo")
async def review_repo(req: RepoScanRequest) -> dict:
    """Scan and review an entire repository's source (within a size budget)."""
    _require_openrouter()

    async with httpx.AsyncClient() as client:
        token = await get_installation_token_for_repo(client, req.repo)
        try:
            snapshot, files, branch = await fetch_repo_snapshot(
                client, token, req.repo, req.branch
            )
        except httpx.HTTPStatusError as exc:
            status = 404 if exc.response.status_code == 404 else 502
            detail = (
                "Repo or branch not found (or the App isn't installed on a "
                "private repo)"
                if status == 404
                else "Failed to read repository"
            )
            logger.error("Failed to read repo: %s", exc)
            raise HTTPException(status_code=status, detail=detail)

        if not snapshot.strip():
            return {"msg": "no reviewable source files found", "branch": branch}

        try:
            review = await call_openrouter(client, REPO_SYSTEM_PROMPT, snapshot)
        except httpx.HTTPStatusError as exc:
            logger.error("OpenRouter error: %s - %s", exc, exc.response.text)
            raise HTTPException(status_code=502, detail="Review generation failed")

    return {
        "review": review,
        "repo": req.repo,
        "branch": branch,
        "files_reviewed": files,
        "model": OPENROUTER_MODEL,
    }
