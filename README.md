# AI PR Reviewer (GitHub App)

A minimal FastAPI service, packaged as a **GitHub App**, that reviews pull
requests with an LLM. When a PR is opened or updated, GitHub delivers a webhook;
the app authenticates as a GitHub App, fetches the diff, sends it to
[OpenRouter](https://openrouter.ai) for review (bugs, security, improvements),
and posts the result back as a PR comment.

## How it works

```
GitHub PR event ──▶ POST /webhook ──▶ verify webhook secret (HMAC)
                                          │
                                          ▼
                          JWT (App key) ──▶ installation access token
                                          │
                                          ▼
                                  fetch diff (GitHub API)
                                          │
                                          ▼
                                  OpenRouter review
                                          │
                                          ▼
                                 post comment on PR
```

Authentication is fully automatic per repository: the app signs a JWT with its
private key, exchanges it for a short-lived **installation access token** scoped
to the repo that fired the event (the installation ID comes from the webhook
payload), and caches that token until it expires. No personal access tokens.

## 1. Register the GitHub App

Go to **Settings → Developer settings → GitHub Apps → New GitHub App** (user
account) or your org's equivalent.

- **GitHub App name:** anything unique.
- **Homepage URL:** any URL (e.g. your repo).
- **Webhook → Active:** checked.
- **Webhook URL:** `https://<your-service>.onrender.com/webhook`
  (you can fill this in after deploying, or use an ngrok URL for local testing).
- **Webhook secret:** generate a random string; save it as `WEBHOOK_SECRET`.
- **Repository permissions:**
  - **Pull requests:** Read & write
  - **Contents:** Read-only
  - **Issues:** Read & write *(PR comments post via the Issues comments API)*
- **Subscribe to events:** **Pull request**.
- **Where can this app be installed:** your choice.

After creating it:

1. Note the **App ID** → `APP_ID`.
2. Under **Private keys**, click **Generate a private key**. A `.pem` downloads
   → `PRIVATE_KEY`.
3. Click **Install App** and install it on the repositories you want reviewed.

## 2. Deploy to Render

The included `render.yaml` is a Render Blueprint — the fastest path is **New →
Blueprint** in the Render dashboard pointed at your fork, then fill in the env
var values when prompted. To set it up manually instead:

1. Push this repo to GitHub and create a new **Web Service** on
   [Render](https://render.com) from it.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
4. **Environment variables:**

   | Key                  | Value |
   | -------------------- | ----- |
   | `APP_ID`             | the App ID from step 1 |
   | `PRIVATE_KEY`        | contents of the `.pem` file (see note below) |
   | `WEBHOOK_SECRET`     | the webhook secret you generated |
   | `OPENROUTER_API_KEY` | from <https://openrouter.ai/keys> |
   | `OPENROUTER_MODEL`   | optional, defaults to `anthropic/claude-sonnet-4` |

   **`PRIVATE_KEY` note:** paste the entire PEM including the
   `-----BEGIN/END-----` lines. Render preserves multi-line values, but if your
   provider does not, replace real newlines with literal `\n` on one line — the
   app converts `\n` back to newlines at load time.

5. Deploy. Once live, set the GitHub App's **Webhook URL** to
   `https://<your-service>.onrender.com/webhook` and confirm the health check at
   `https://<your-service>.onrender.com/` returns `{"status":"ok"}`.

Open, reopen, or push to a PR in an installed repo — the AI review appears as a
comment within a few seconds.

## General review endpoints

Besides the PR webhook, the service exposes three on-demand review endpoints
that reuse the same OpenRouter logic. They return JSON (`{"review": "..."}`)
rather than posting a comment.

| Endpoint | Body | What it reviews |
| -------- | ---- | --------------- |
| `POST /review/code` | `{"code": "...", "language": "python", "filename": "x.py"}` | A raw code snippet (`language`/`filename` optional) |
| `POST /review/commit` | `{"repo": "owner/name", "sha": "<commit-sha>"}` | The diff of a single commit |
| `POST /review/repo` | `{"repo": "owner/name", "branch": "main"}` | A snapshot of the repo's source (`branch` optional) |

```bash
curl -X POST https://<your-service>.onrender.com/review/code \
  -H 'Content-Type: application/json' \
  -d '{"code":"def div(a,b):\n    return a/b","language":"python"}'
```

For `commit` and `repo`, the service uses a GitHub App installation token when
the App is installed on the target repo (needed for private repos); otherwise it
falls back to unauthenticated access, which works for public repos. The repo
scan concatenates source files up to ~60k characters, so very large repos are
sampled rather than read in full.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # fill in APP_ID, PRIVATE_KEY, WEBHOOK_SECRET, OPENROUTER_API_KEY
uvicorn app:app --reload --port 8000
```

GitHub needs a public URL to deliver webhooks. The simplest option is a
Cloudflare quick tunnel (no account, no signup):

```bash
cloudflared tunnel --url http://localhost:8000
```

It prints a `https://<random>.trycloudflare.com` URL — point the App's Webhook
URL at `https://<random>.trycloudflare.com/webhook`. The URL changes each time
you restart cloudflared, so update the App's Webhook URL if you restart it.
Redeliver past payloads from the App's **Advanced → Recent Deliveries** tab for
fast iteration. (`ngrok http 8000` also works if you prefer it and it isn't
flagged by your antivirus.)

## Notes

- Only `opened`, `reopened`, `synchronize`, and `ready_for_review` actions
  trigger a review; other events are acknowledged and ignored.
- Diffs larger than ~60k characters are truncated before being sent to the model.
- The comment is posted to the PR conversation via the Issues comments API.
