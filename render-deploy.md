# Deploying to Render — step by step

This repo already has everything Render needs to build and run the service:
- `Dockerfile` (Render reads `${PORT}`, binds `0.0.0.0`, runs `uvicorn`, has a `HEALTHCHECK`)
- `requirements.txt` (pinned)
- `render.yaml` (Render Blueprint that pins branch, env vars, health check)

The deploy is a one-time dashboard configuration; afterward every push to the
`newok` branch auto-rebuilds.

## 1. Push the branch and the Blueprint

The repo currently has untracked files in `tests/my_sample_cases/`. Commit
those (or add them to `.gitignore`) so the working tree is clean — Render
clones HEAD of the branch.

```bash
git add render.yaml tests/my_sample_cases/
git commit -m "Add Render Blueprint and personal sample cases"
git push origin newok
```

> The pre-existing `server.log`, `server.out.log`, `server.err.log` are already
> matched by `*.log` in `.gitignore` and will not be committed.

## 2. Connect the repo on Render

1. Open https://dashboard.render.com/blueprints → **New Blueprint Instance**.
2. Pick the GitHub repo (`SUST-VibeJS-BUP-Hackathon-Preli`), branch `newok`.
3. Render parses `render.yaml` and shows one service: **`gridwise-api`**.
4. Click **Apply**. Render starts the first build (no secrets yet — the build
   itself doesn't need `OPENAI_API_KEY`, only the running container does).

## 3. Add the OpenAI secret

Once the Blueprint finishes applying:

1. Go to **Dashboard → gridwise-api → Environment**.
2. Find `OPENAI_API_KEY` (Render marked it as "secret" because of the name).
3. Click **Edit** and paste your real `sk-...` value.
4. Save. Render restarts the service automatically.

Without this key the service still runs (good for smoke testing `/health`)
but every optimization will fall back to the emergency classifier and you will
fail the hidden LLM-path scoring.

## 4. Confirm the deploy is live

Wait for the build to finish (~2–4 min for first build; subsequent rebuilds
~30s). The service URL looks like `https://gridwise-api-xxxx.onrender.com`.

Health probe:
```bash
curl https://gridwise-api-xxxx.onrender.com/health
# expected: {"status":"ok"}
```

Run the public sample against the deployed host:
```bash
python3 tests/public_cases.py --base-url https://gridwise-api-xxxx.onrender.com
```

All 10 cases must report `PASS`.

## 5. Submit to the judges

The URL to register with the organizers is:
```
https://gridwise-api-xxxx.onrender.com/optimize-energy
```
with `POST` + `Content-Type: application/json` (the judge handles headers).

## Plan notes / things to know

- **Plan:** `starter` ($7/mo) keeps the container warm. The free plan spins
  down after 15 min idle, which adds ~30 s to the next cold start — risky for
  a 30 s judge budget.
- **Region:** default `oregon`. If your OpenAI project is geo-restricted,
  change the region to one closer to your account's home.
- **Workers:** the Dockerfile runs uvicorn with `--workers 1`. The
  interpreter's in-process cache and the LP are single-threaded; horizontal
  scaling is via Render instances if you ever need it (not needed for the
  preliminary).
- **Timeouts:** `OPENAI_TIMEOUT_S=10`, `OPENAI_FALLBACK_TIMEOUT_S=8`,
  `REQUEST_BUDGET_S=26` (under the 30 s judge limit). The ladder skips the
  corrective retry when the deadline is inside 3 s, so worst-case wall time
  is ~18 s.
- **Rebuilds:** every push to `newok` triggers a rebuild (auto-deploy is on).
- **Logs:** Dashboard → gridwise-api → Logs streams stdout/stderr from
  uvicorn. Logs are kept for 7 days.

## If Render can't reach OpenAI from the region you pick

Add a gateway via `OPENAI_BASE_URL` in the Environment tab (e.g. a Cloudflare
Worker or LiteLLM proxy pointing at OpenAI, or a compatible provider that
supports `response_format=json_schema`). Then redeploy. The model names stay
the same; only the base URL changes.
