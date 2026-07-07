# Deal Finder

Watch Swiss second-hand marketplaces for the car you want — get notified about new
listings, translated to English and pre-analyzed by a **free local AI**.

![Python 3.13](https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-web%20UI%20%2B%20API-009688?logo=fastapi&logoColor=white)
![Ollama](https://img.shields.io/badge/AI-local%20via%20Ollama-black?logo=ollama)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

- **Scans on your schedule** — tutti.ch, Ricardo.ch, AutoScout24.ch, Facebook Marketplace.
- **Notifies once per listing** — email or Telegram, deduplicated across marketplaces.
- **AI-enriched** — translates DE/FR/IT → EN and answers your questions (condition,
  known issues, pickup, …) using [Ollama](https://ollama.com). No API keys, no cost.
- **Extensible** — new item types (houses, phones, …) and new marketplaces are one file each.

## Quick start

**1. Install prerequisites** (macOS shown; any OS with Python 3.13 works):

```bash
brew install python@3.13 pipenv
```

**2. Install the app:**

```bash
git clone <this-repo> && cd deal_finder
export PIPENV_VENV_IN_PROJECT=1   # keep the virtualenv in ./.venv
pipenv install --dev
cp .env.example .env              # optional — everything can be set later in the UI
```

**3. (Optional) Enable local AI:**

```bash
brew install ollama
brew services start ollama
ollama pull gemma3:4b
```

**4. Run it:**

```bash
pipenv run dev
```

Open <http://127.0.0.1:8000>, create a watch (make, model, price/year filters, where to
notify you, which marketplaces), then click **Run now**.

> **First time?** Pick the **Demo** marketplace — it exercises the whole pipeline with
> canned listings and zero network calls. On any watch, **Run now** offers a
> *test notification* checkbox and a *dry run* checkbox (opens matches in browser tabs
> instead of notifying).

## Commands

| Command | What it does |
|---|---|
| `pipenv run dev` | Start with auto-reload at <http://127.0.0.1:8000> |
| `pipenv run start` | Start without reload (production-ish) |
| `pipenv run test` | Run the test suite (fully offline) |
| `pipenv run fb-login` | One-time Facebook login (stores a session, never your password) |
| `pipenv run telegram-chat-id` | Helper to find your Telegram chat ID |

## Marketplaces

| Marketplace | Access | Setup | Notes |
|---|---|---|---|
| **tutti.ch** | Public GraphQL API via [tutti-scraper](https://pypi.org/project/tutti-scraper/) | none | Searches tutti's `cars` category with structured make/year/mileage. |
| **Ricardo.ch** | Bundled Camoufox browser via [ricardo-scraper](https://pypi.org/project/ricardo-scraper/) | none | Handles Cloudflare itself, fully self-contained. |
| **AutoScout24.ch** | Public JSON API via [autoscout24-scraper](https://pypi.org/project/autoscout24-scraper/) | none | Biggest Swiss car inventory; no anti-bot measures at all. |
| **Facebook Marketplace** | Playwright browser via [facebook-marketplace-scraper](https://pypi.org/project/facebook-marketplace-scraper/) | `pipenv run fb-login` once | **Automating Facebook violates its ToS and risks an account ban** — use a dedicated account. |
| **Demo** | Canned offline listings | none | Try the full pipeline without any network. |

Each adapter wraps a dedicated PyPI package that manages its own site access —
deal_finder drives no browser of its own. One marketplace failing (block, network,
markup change) never aborts a run; it's logged and retried on the next schedule.

## Configuration

Everything is configurable in the **Settings** page or via `.env` — see
[.env.example](.env.example) for the annotated full list. The groups that matter most:

- `DF_SMTP_*` — email notifications (for Gmail, use an App Password)
- `DF_OLLAMA_*` — AI base URL and model (default `gemma3:4b`)
- `DF_ADAPTER_*_ENABLED` — turn individual marketplaces on/off

## Local AI

AI is optional and never blocks: if Ollama is off or unreachable, notifications still go
out — just untranslated and without answers. The default `gemma3:4b` handles DE/FR/IT→EN
translation and listing Q&A well on an 8 GB Mac. Set the model via `DF_OLLAMA_MODEL` or
in **Settings**.

<details>
<summary><strong>Choosing a model for your system RAM</strong> (recommendations as of July 2026)</summary>

Pick the largest model that fits your RAM at Q4 quantization, leaving headroom for the
OS and the app. From the 16 GB tier up, multilingual comprehension improves noticeably
over the default `gemma3:4b`.

| System RAM | Best model (Q4) | Model size | License | Why this pick |
|---|---|---|---|---|
| 8 GB | phi4-mini (3.8B) or qwen3:4b | ~2.5 GB | MIT / Apache 2.0 | Best reasoning per GB; 3-4B models are the max for responsive use on 8GB |
| 12 GB | llama3.3:8b or qwen3:8b | ~5 GB | Llama Community / Apache 2.0 | First tier where 8B-class models fit comfortably; better comprehension than 3-4B |
| 16 GB | gemma4:12b or gpt-oss:20b | 7.6 / 14 GB | Apache 2.0 | Gemma 4 12B runs in 16GB; gpt-oss:20b fits but tight |
| 24 GB | gemma4:26b or qwen3.6:35b-a3b | ~16-18 GB | Apache 2.0 | Practical floor for 30B-class models; big quality jump for document Q&A |
| 32 GB | qwen3.6:35b-a3b (or gemma4:26b with headroom) | ~22 GB | Apache 2.0 | Both load with 64K context; Qwen3.6 faster (51 vs 31 tok/s); Gemma leaves more headroom |
| 48 GB | qwen3:32b Q8 or gemma4:31b higher quant | ~33 GB | Apache 2.0 | Higher-precision 30B-class; Gemma 4 31B at 84.3% GPQA Diamond is frontier-adjacent |
| 64 GB | llama3.3:70b or gpt-oss:120b (tight) | ~42 GB | Llama Community / Apache 2.0 | Llama 3.3 70B Q4 needs 42GB; the classic serious tier for nuanced Q&A |
| 96 GB | gpt-oss:120b or llama4:scout | ~62-65 GB | Apache 2.0 / Llama Community | 120B-class with comfortable KV-cache headroom; Scout for very long documents (10M context) |
| 128 GB | deepseek-v4-flash or llama4:maverick | ~80-95 GB | MIT / Llama Community | DeepSeek V4 Flash (284B MoE ~80GB) and Maverick (400B MoE ~95GB); Mac Studio M4 Max territory |
| 192 GB | qwen3:235b-a22b Q4-Q5 | ~130-145 GB | Apache 2.0 | Leads open reasoning (77.2% GPQA Diamond); room for large context |
| 256 GB | qwen3 235B Q6/Q8 or GLM-class MoE Q4 | ~180-230 GB | Apache 2.0 / MIT | Higher-precision flagship MoE; beats older Llama 405B |
| 384-512 GB | deepseek-v3.2 / V4-class 671B+ MoE | ~380-410 GB Q4 | MIT | Strongest open model you can host at home; full Q4 quality needs ~512GB |

</details>

## How it works

```
Web UI / API ──> Watch (DB) ──> APScheduler (per-watch interval/cron) ──> run_watch()

run_watch:  adapters.search() ─> filter (price/year/km/keywords/location) ─> dedup
            ─> AI translate + answer questions ─> notify (email/Telegram) ─> record seen
```

- **Seed mode** — a watch's first run marks existing listings as seen without notifying,
  so you aren't flooded with old inventory.
- **Dedup** — one notification per listing per watch, plus a cross-marketplace
  title+price heuristic so the same car isn't sent twice.
- **Resilience** — adapter failures are isolated per site; failed notifications leave the
  listing unseen so it retries next run.

## Extending

- **New item type** (house, phone, …): subclass `BaseCategory` in
  [deal_finder/categories/](deal_finder/categories/) and register it in
  [registry.py](deal_finder/registry.py) — forms, filters, and default questions are
  generated from the field definitions.
- **New marketplace**: subclass `BaseAdapter`, implement `search()`, register it.
  See [adapters/ricardo.py](deal_finder/adapters/ricardo.py) (~15 lines) and
  [adapters/demo.py](deal_finder/adapters/demo.py).

## Project layout

```
deal_finder/
  main.py             FastAPI app + scheduler lifespan
  registry.py         category + adapter registries
  pipeline.py         the scan → filter → dedup → AI → notify flow
  categories/         item-type definitions (car.py, …)
  adapters/           one file per marketplace
  ai/                 Ollama client, translation, question answering
  notify/             email, Telegram, browser-open (dry run)
  web/                routes, JSON API, templates
tests/                offline test suite (pytest)
deploy/               LaunchAgent, pmset wake script, deploy guide
```

## Deployment

Scans only run while the app process is alive and the machine is awake. See
[deploy/README_deploy.md](deploy/README_deploy.md) for running it as a macOS
LaunchAgent with scheduled wakes, or 24/7 on an always-on host (Pi/VPS) under systemd.

## Legal

Personal monitoring tool — scan modestly and responsibly. Marketplace internals are
unofficial and may change or block access at any time. Automating Facebook breaks its
Terms of Service and can get the account locked or banned; use a dedicated account.
