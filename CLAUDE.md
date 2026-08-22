# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running it

```bash
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

The site is then at `http://localhost:8000` — the marketing page at `/`, the tools at `/app`, `/anomaly`, `/compare`, `/format`, `/codebook`, and sign-in at `/signin/`. `.claude/launch.json` holds the same command for the preview tooling.

The app boots without any secrets: `.env` is loaded when present, and the pieces that need keys degrade quietly rather than crash. Without Stripe keys `/billing/*` is inert, without OAuth credentials `/auth/providers` returns an empty list, and without `RESEND_API_KEY` the email sign-in link is printed to the server log instead of sent. So a clean checkout can run and be clicked through; only flows that genuinely need a provider require setup.

There is no test suite, no linter config, and no frontend build step.

Two generators, run by hand when their inputs change, not in any pipeline:

```bash
python make_sample_files.py    # the sample .xlsx/.docx under static/marketing/samples/
python make_og_images.py       # the og-*.png social cards in static/marketing/
```

## Architecture

**One FastAPI app, five tools, no framework on the front end.** `main.py` owns the Excel-to-Word endpoints (`/upload`, `/columns`, `/generate`) directly and includes a router per feature: `compare.py`, `format_report.py`, `anomaly_detector.py`, `codebook.py`, plus `auth.py`, `email_auth.py`, `billing.py`, `analytics.py`. Each tool's real work lives in a plain module beside its router — `converter.py` and `diff_engine.py` are pure logic with no FastAPI in them.

**Routers and static mounts share the same paths, and order decides the winner.** `/compare`, `/format`, `/anomaly`, and `/codebook` are each both an API prefix and a `StaticFiles` mount serving that tool's page. `main.py` includes the routers *before* mounting the directories, so the API routes match first. A bare `@app.get("/app")` (and one per tool) exists because a mount only matches `/app/...`, not `/app` — it redirects to the mount. Adding a route under one of these prefixes means checking it doesn't collide with a filename, and keeping the include-before-mount order intact. The catch-all mount of `static/marketing` at `/` must stay last.

**Every generating tool goes through `gating.py`.** `gate(request, tool)` raises 402 unless the caller is either an anonymous visitor with free files left (`usage.py`, 3 per tool per month, keyed on a salted IP hash) or a signed-in subscriber (`billing.py`). One subscription unlocks every tool; the free allowances are independent per tool. `max_upload_bytes()` is where the 10 MB / 25 MB split in the pricing copy is enforced. New tools call `gate` then `record_use` — don't reimplement the rule.

**Storage.** `users.db` (SQLite) holds accounts, the anonymous usage counters, and the `usage_events` analytics table; `auth.py`, `usage.py`, and `analytics.py` all open the same file. Uploads and generated documents in `uploads/` and `outputs/` are session artifacts, not records: a sweep in `main.py` deletes them after an hour, except Qualitative Coding corpus `.json` files, which use the longer `codebook.CORPUS_MAX_AGE_SECONDS` because tagging a corpus outlives the hour.

**`analytics.log_event`** records both successes and failures per tool, and `/admin/stats` is restricted to the addresses in `ADMIN_EMAILS` (unset means the route 404s for everyone).

## The front end

Seven standalone HTML files under `static/` — marketing, five tools, sign-in — each carrying its own inline `<style>` and `<script>`. No bundler, no shared stylesheet, no npm.

That means **the design system is duplicated in seven `:root` blocks**, and a token change has to be applied to all seven or the site drifts. The contract in those tokens:

- Each accent has a bright value and a darker `-ink` twin. The brights (`--azure` `#2E90FA`, `--emerald` `#12B76A`, `--amber-lit` `#F79009`, `--coral`, `--violet`) all fall under 3.2:1 on white, so they are for decoration only — tab strips, icon fills, borders, the logo cells. Anything carrying text or sitting behind white uses the `-ink` twin. `--ink-faint` `#667085` is the lightest grey allowed to be text; `--hairline` `#98A2B3` is for rules and dots.
- Type has three roles: Space Grotesk at 600 with `-0.02em` tracking for headings, Inter for prose and UI, JetBrains Mono for short labels and for values out of the user's own spreadsheet — not for paragraphs.
- Tappable controls get a 44px minimum hit area.

The tool pages read their state from the same endpoints the marketing page does (`/me`, `/billing/status`, `/auth/providers`) and fail quietly when those are unreachable.

## Config and deploy

`.env` is local-only and gitignored, along with `users.db`, `uploads/`, and `outputs/`; `.env.example` documents every variable. Production is Render, which sets real environment variables in its dashboard and never reads `.env`. `Procfile` is the production command.

**Render deploys from `main`, so a push to `main` ships the site.** Work that should be reviewed before it goes live belongs on a branch and a pull request.

`main.py` 301-redirects the old `dataexact.co.uk` domain to `www.dataexact.io`.
