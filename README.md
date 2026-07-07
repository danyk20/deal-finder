# Deal Finder

Periodically scans second-hand marketplaces for a specific item (starting with a
**Tesla Model S** on the Swiss market), and when a new matching listing appears it emails
you the link, the description **translated to English**, and **AI-inferred answers** to
questions you define (condition, known issues, pickup, …). The AI runs **locally and
free** via [Ollama](https://ollama.com) — no API tokens.

Controlled through a small **FastAPI** app with a **server-rendered web UI** (Jinja2 +
HTMX) and a JSON API. Built to extend to new item types (houses, phones, …) and new
marketplaces by dropping in one file.

> **Status:** framework, matching, dedup, scheduling, local-AI enrichment, email,
> Telegram, API and UI are complete and tested. Every marketplace adapter wraps a
> dedicated PyPI package that manages its own access internally — deal_finder itself
> drives no browser at all anymore. **tutti.ch** and **AutoScout24** call the sites' own
> public GraphQL/JSON APIs directly via
> [tutti-scraper](https://pypi.org/project/tutti-scraper/) and
> [autoscout24-scraper](https://pypi.org/project/autoscout24-scraper/) — no browser or
> bypass needed at all, both verified end-to-end. **Ricardo.ch** uses
> [ricardo-scraper](https://pypi.org/project/ricardo-scraper/), which drives its own
> bundled anti-fingerprinting Camoufox browser to get past Cloudflare's Managed
> Challenge. **Facebook** drives its own separate Playwright browser and login flow via
> [facebook-marketplace-scraper](https://pypi.org/project/facebook-marketplace-scraper/)
> — on by default, needs a one-time login, and carries ToS/ban risk. See
> [Marketplaces](#marketplaces).

## Quick start

This project uses **[Pipenv](https://pipenv.pypa.io)** (Python 3.11+; 3.13 recommended).

```bash
cd deal_finder
export PIPENV_VENV_IN_PROJECT=1            # keep the virtualenv in ./.venv (recommended)
pipenv install --dev                       # create the venv + install deps

cp .env.example .env                       # edit SMTP + Ollama (or set them later in the UI)

pipenv run dev                             # start the app at http://127.0.0.1:8000 (--reload)
pipenv run test                            # run the test suite (offline)
```

`pipenv shell` drops you into the environment (then `uvicorn …`, `pytest` work directly).
Handy shortcuts are defined in the `Pipfile` `[scripts]`: `dev`, `start`, `test`,
`fb-login`, `telegram-chat-id` — run any with `pipenv run <name>`.

Facebook Marketplace needs a one-time manual login (stores no password):
`pipenv run fb-login`. tutti, Ricardo and AutoScout24 need no setup at all — each calls
its own dedicated package directly.

Open http://127.0.0.1:8000, create a watch (Make `Tesla`, Model `Model S`, price/year
filters, your email or Telegram, pick marketplaces), then **Run now**. Use the **Demo**
marketplace to see the whole pipeline offline without any network calls.

On the watch page, **Run now** has two independent checkboxes: **send test notification**
(sends via the watch's chosen channel — email or Telegram — like a real scan) and
**dry run** (opens every match in a new local browser tab instead — no notification, no
AI enrichment, nothing written to the database; dry run always wins if both are
checked). Handy for eyeballing results for a brand-new watch before trusting it to
notify you.

> Scans run at each watch's schedule; when a site blocks a run it's logged and retried
> next time. Lower the scan frequency (e.g. daily) to stay under rate limits.

### Local AI (Ollama)

```bash
brew install ollama
ollama serve                  # or: brew services start ollama
ollama pull gemma3:4b         # verify the exact tag with `ollama list`
```
Set the model in **Settings** (`DF_OLLAMA_MODEL`). If Ollama is off or unreachable, the
app still emails matches — just with the original text and no AI answers (it never blocks
on AI). Gemma handles DE/FR/IT→EN translation and answering from listing text well on a Mac.

### Email

Set SMTP in **Settings** or `.env` (`DF_SMTP_*`). For Gmail, use an App Password.

## How it works

```
Web UI / API  ──>  Watch (DB)  ──>  APScheduler (per-watch interval/cron)  ──>  run_watch()

run_watch:  adapters.search()  ─>  filter (price/year/km/keywords/location)  ─>  dedup
            ─>  AI translate + answer questions  ─>  email (digest)  ─>  record (SeenListing)
```

- **Seed mode**: a watch's *first* run records existing listings as "seen" without
  emailing, so you don't get flooded with old inventory. Only genuinely new listings
  afterward trigger emails. (Toggle in Settings.)
- **Dedup**: a listing is emailed once per watch (`watch_id + marketplace + external_id`),
  plus a cross-marketplace title+price heuristic to avoid double-emailing the same car.
- **Resilience**: one marketplace failing (network/bot-block) never aborts a run — its
  error is recorded and the others proceed. If email fails, the listing stays "unseen" and
  is retried next run.

## How marketplace scanning works

deal_finder drives no browser of its own. Every adapter is a thin wrapper around a
dedicated PyPI package that manages its own access to the target site (a public
JSON/GraphQL API, or — for sites that need it — that package's own bundled/patched
browser). The adapter maps whatever that package returns into a `Listing` and hands off
to the same matching / dedup / AI / notify pipeline, regardless of source.

If a site's package raises (network error, bot-block, parse problem), the pipeline
records it per-adapter and isolates it — other sites still scan, and it retries next
schedule. Tunables live in **Settings** / `.env` (`DF_BROWSER_MAX_ITEMS_PER_RUN`, plus
`DF_ADAPTER_*_ENABLED` per site).

## Extending it

- **New item type** (house, phone, …): add a `BaseCategory` subclass in
  `deal_finder/categories/` and register it in `registry.py`. The web form, filters, and
  default questions are generated from the category's field definitions — no UI changes.
- **New marketplace**: subclass `BaseAdapter`, implement `search()` (wrapping a
  dedicated package or calling an API directly), and register it in `registry.py`.
  Matching, dedup, AI, notifications, scheduling, and UI all work unchanged.

See `deal_finder/adapters/ricardo.py` (a ~15-line adapter) and `deal_finder/adapters/demo.py`.

## Marketplaces

| Adapter | State | Notes |
|---|---|---|
| **tutti.ch** | ✅ verified end-to-end | Uses the [tutti-scraper](https://pypi.org/project/tutti-scraper/) package, which calls tutti's own **public GraphQL API** (`tutti.ch/api/v10/graphql`) directly — plain requests, no browser or bypass. Pins the search to tutti's `cars` category (skips toys/accessories) and reads structured make/year/mileage properties. |
| **Ricardo.ch** | ✅ verified end-to-end | Uses the [ricardo-scraper](https://pypi.org/project/ricardo-scraper/) package, which drives its own bundled, anti-fingerprinting Camoufox browser to get past Cloudflare's Managed Challenge — self-contained, no shared setup or profile needed. Free-text search returns some accessories too — add price/keyword filters to narrow. |
| **AutoScout24.ch** | ✅ verified end-to-end | Biggest Swiss car inventory. Uses the [autoscout24-scraper](https://pypi.org/project/autoscout24-scraper/) package, which calls `api.autoscout24.ch` — the site's own **public, unauthenticated JSON API** — directly. No browser, no Cloudflare/Akamai to bypass, no anti-bot measures needed at all. |
| **Facebook Marketplace** | ⚠️ on by default; needs login | Uses the [facebook-marketplace-scraper](https://pypi.org/project/facebook-marketplace-scraper/) package, which drives its own dedicated Playwright browser directly against facebook.com (no public API exists). Log in once via `pipenv run fb-login` (stores no password) — its session is saved inside that package's own installed directory, so a `pipenv sync`/reinstall wipes it and you'll need to log in again. **Automating Facebook violates its ToS and risks account lock/ban** — use a dedicated account. Note: currently conflicts with ricardo-scraper's exact `playwright==1.60.0` pin (facebook-marketplace-scraper needs `playwright~=1.61`) — not installed by default; see the Pipfile comment. |
| **Demo** | ✅ offline | Canned multilingual sample listings; exercises the whole pipeline with no network calls. |

Per-site URL patterns / selectors are marked `# VERIFY LIVE` in each adapter — the one
place to adjust if a site changes its markup.

## Legal / ToS

This is a personal monitoring tool. It scans infrequently and relies on each
marketplace's own dedicated package to access it responsibly. Marketplace internals are
unofficial and may change or block access. **Facebook**: automating it breaks Facebook's
Terms and can get your account locked or banned — it's on by default per configuration,
but use a dedicated account and understand the risk. Storing your Facebook password in
Settings is optional and less safe than the one-time manual login. Use responsibly; keep
scan frequency modest.

## Project layout

```
deal_finder/
  main.py            FastAPI app + scheduler lifespan
  config.py db.py models.py schemas.py service.py progress.py
  registry.py        category + adapter registries
  scheduler.py pipeline.py matching.py util.py
  categories/        base.py, car.py            (add house.py, phone.py …)
  adapters/          base.py, demo.py, tutti.py, ricardo.py, autoscout24.py, facebook.py
  browser/           extract.py (shared price/year/mileage parsing), fb_login.py
  ai/                client.py, translate.py, questions.py
  notify/            email.py, telegram.py, telegram_setup.py, browser_open.py,
                     templates/match_email.html
  web/               api.py, routes.py, templates/
tests/               matching, adapters, browser_extract, ai, notify, pipeline, api
                     (+ fixtures)
deploy/              launchd plist, pmset wake script, deploy guide
```

## Deployment / running in the background

See [deploy/README_deploy.md](deploy/README_deploy.md). Short version: install the
LaunchAgent + run Ollama as a service + schedule `pmset` wakes (free, local, best while on
power), or run on an always-on host (Pi/VPS) for true 24/7.
