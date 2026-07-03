# Deal Finder

Periodically scans second-hand marketplaces for a specific item (starting with a
**Tesla Model S** on the Swiss market), and when a new matching listing appears it emails
you the link, the description **translated to English**, and **AI-inferred answers** to
questions you define (condition, known issues, pickup, …). The AI runs **locally and
free** via [Ollama](https://ollama.com) — no API tokens.

Controlled through a small **FastAPI** app with a **server-rendered web UI** (Jinja2 +
HTMX) and a JSON API. Built to extend to new item types (houses, phones, …) and new
marketplaces by dropping in one file.

> **Status:** framework, matching, dedup, scheduling, local-AI enrichment, email, API and
> UI are complete and tested. **tutti.ch** and **Ricardo** are scanned by **driving a real
> Chrome like a human** (via **patchright** — a patched Playwright that evades bot
> detection — with a persistent profile, opening the search page then each listing one at
> a time with random delays; tutti verified end-to-end, Ricardo works but may rate-limit
> under frequent access). **AutoScout24** and **Facebook** each wrap a dedicated PyPI
> package that manages its own access: AutoScout24 calls the site's own public JSON API
> directly ([autoscout24-scraper](https://pypi.org/project/autoscout24-scraper/)) — no
> browser or bypass needed at all; Facebook drives its own separate Playwright browser and
> login flow ([facebook-marketplace-scraper](https://pypi.org/project/facebook-marketplace-scraper/))
> — on by default, needs a one-time login, and carries ToS/ban risk. See
> [Marketplaces](#marketplaces).

## Quick start

This project uses **[Pipenv](https://pipenv.pypa.io)** (Python 3.11+; 3.13 recommended).

```bash
cd deal_finder
export PIPENV_VENV_IN_PROJECT=1            # keep the virtualenv in ./.venv (recommended)
pipenv install --dev                       # create the venv + install deps (patchright, facebook-marketplace-scraper, ...)
pipenv run browsers                        # = patchright install chrome (real Chrome channel)

cp .env.example .env                       # edit SMTP + Ollama (or set them later in the UI)

pipenv run dev                             # start the app at http://127.0.0.1:8000 (--reload)
pipenv run test                            # run the test suite (offline; no browser needed)
```

`pipenv shell` drops you into the environment (then `uvicorn …`, `pytest` work directly).
Handy shortcuts are defined in the `Pipfile` `[scripts]`: `dev`, `start`, `test`,
`browsers`, `fb-login`, `solve` — run any with `pipenv run <name>`.

If tutti or Ricardo ever shows a "checking your browser" / "I'm not a robot" step, clear
it **once** yourself in a visible window — the cleared session persists in the browser
profile and scheduled scans reuse it: `pipenv run solve <marketplace>`. Deal Finder never
solves challenges itself. AutoScout24 (public API) and Facebook (its own dedicated
package — see below) don't use this shared browser session, so this never applies to them.

Open http://127.0.0.1:8000, create a watch (Make `Tesla`, Model `Model S`, price/year
filters, your email, pick marketplaces), then **Run now**. Use the **Demo** marketplace to
see the whole pipeline offline without a browser.

On the watch page, **Run now** has two independent checkboxes: **send test email**
(emails the matches, like a real scan) and **dry run** (opens every match in a new local
browser tab instead — no email, no AI enrichment, nothing written to the database; dry
run always wins if both are checked). Handy for eyeballing results for a brand-new watch
before trusting it to email you.

> **Browser scanning** opens a **visible Chrome window** during each scan (best for bypassing
> bot detection). Keep the Mac awake and logged in. Scans run at each watch's schedule; when
> a site blocks a run it's logged and retried next time. Lower the scan frequency (e.g. daily)
> to stay under rate limits.

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

## How marketplace scanning works (browser-driven)

Direct HTTP requests to these sites are bot-blocked (HTTP 403). Instead, each adapter
drives a **real headful Chrome** via Playwright (`deal_finder/browser/`):

1. Open a **persistent Chrome profile** (`~/.deal_finder/profiles/<site>`) — cookies and a
   warmed profile persist across runs, which is what defeats bot detection.
2. Navigate to the site's search page for your query.
3. Collect listing links, then **open each listing one at a time** with a random
   few-second delay between them — like a person browsing.
4. Extract fields from each detail page (JSON-LD → OpenGraph meta → DOM), map to a
   `Listing`, and hand off to the same matching / dedup / AI / email pipeline.

If a site shows a bot-wall/CAPTCHA/login page, the adapter raises a typed error that the
pipeline records and isolates — other sites still scan, and it retries next schedule.
Tunables live in **Settings** / `.env` (`DF_BROWSER_*`): headless on/off, items per run,
min/max delay, per-site enable.

## Extending it

- **New item type** (house, phone, …): add a `BaseCategory` subclass in
  `deal_finder/categories/` and register it in `registry.py`. The web form, filters, and
  default questions are generated from the category's field definitions — no UI changes.
- **New marketplace**: subclass `CarBrowserAdapter` (in `deal_finder/adapters/_browser_car.py`)
  — set `base_url`, `id_regex`, and `build_search_urls` — then register it. Or subclass
  `BaseAdapter` directly for a non-browser source. Matching, dedup, AI, email, scheduling,
  and UI all work unchanged.

See `deal_finder/adapters/ricardo.py` (a ~15-line adapter) and `deal_finder/adapters/demo.py`.

## Marketplaces

| Adapter | State | Notes |
|---|---|---|
| **tutti.ch** | ✅ verified end-to-end | Real headful Chrome; opens each car listing one at a time. Restricts to the `/autos/` category (skips toys/accessories). Verified live 2026. |
| **Ricardo.ch** | ✅ working (may rate-limit) | Verified extraction (search cards + detail). Free-text search returns some accessories too — add price/keyword filters to narrow. Frequent access can trigger a temporary 403 (logged, retried next run). |
| **AutoScout24.ch** | ✅ verified end-to-end | Biggest Swiss car inventory. Uses the [autoscout24-scraper](https://pypi.org/project/autoscout24-scraper/) package, which calls `api.autoscout24.ch` — the site's own **public, unauthenticated JSON API** — directly. No browser, no Cloudflare/Akamai to bypass, no anti-bot measures needed at all. |
| **Facebook Marketplace** | ⚠️ on by default; needs login | Uses the [facebook-marketplace-scraper](https://pypi.org/project/facebook-marketplace-scraper/) package, which drives its own dedicated Playwright browser directly against facebook.com (no public API exists). Log in once via `python -m deal_finder.browser.fb_login` (stores no password) — its session is saved inside that package's own installed directory, so a `pipenv sync`/reinstall wipes it and you'll need to log in again. **Automating Facebook violates its ToS and risks account lock/ban** — use a dedicated account. |
| **Demo** | ✅ offline | Canned multilingual sample listings; exercises the whole pipeline with no browser. |

Per-site URL patterns / selectors are marked `# VERIFY LIVE` in each adapter — the one
place to adjust if a site changes its markup.

## Legal / ToS

This is a personal monitoring tool. It scans infrequently, paces requests with human-like
delays, and reuses a real browser profile. Marketplace internals are unofficial and may
change or block access. **Facebook**: automating it breaks Facebook's Terms and can get
your account locked or banned — it's on by default per configuration, but use a dedicated
account and understand the risk. Storing your Facebook password in Settings is optional and
less safe than the one-time manual login. Use responsibly; keep scan frequency modest.

## Project layout

```
deal_finder/
  main.py            FastAPI app + scheduler lifespan
  config.py db.py models.py schemas.py service.py
  registry.py        category + adapter registries
  scheduler.py pipeline.py matching.py util.py
  categories/        base.py, car.py            (add house.py, phone.py …)
  adapters/          base.py, demo.py, _browser_car.py, tutti.py, ricardo.py,
                     autoscout24.py, facebook.py
  browser/           session.py (headful Chrome), stealth.py, human.py, extract.py,
                     detect.py, adapter.py (BrowserAdapter), fb_login.py
  ai/                client.py, translate.py, questions.py
  notify/            email.py, templates/match_email.html
  web/               api.py, routes.py, templates/
tests/               matching, adapters, browser_extract, browser_adapter, ai,
                     notify, pipeline, api  (+ fixtures)
deploy/              launchd plist, pmset wake script, deploy guide
```

## Deployment / running in the background

See [deploy/README_deploy.md](deploy/README_deploy.md). Short version: install the
LaunchAgent + run Ollama as a service + schedule `pmset` wakes (free, local, best while on
power), or run on an always-on host (Pi/VPS) for true 24/7.
