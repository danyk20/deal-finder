# Deployment — running Deal Finder in the background

The scheduler lives inside the app process, so "running in the background" means keeping
that process alive and making sure the machine is awake when scans are due. Pick the
option that matches how reliable you need scanning to be.

## Option A — macOS, runs in the background (recommended for a laptop)

Free and fully local. Good when the Mac is on charger most of the time.

1. **Run Ollama as a service** so the model is always available:
   ```bash
   brew services start ollama
   ollama pull gemma3:4b        # verify the tag with `ollama list`
   ```
2. **Install the app as a LaunchAgent** (auto-start at login, auto-restart on crash):
   ```bash
   # one-time: create the in-project virtualenv the LaunchAgent uses
   PIPENV_VENV_IN_PROJECT=1 pipenv install --dev
   # edit paths in deploy/com.dealfinder.plist if your project or pipenv live elsewhere
   cp deploy/com.dealfinder.plist ~/Library/LaunchAgents/com.dealfinder.plist
   launchctl load -w ~/Library/LaunchAgents/com.dealfinder.plist
   ```
   The plist runs `pipenv run uvicorn …` in the project dir. The UI is now at
   http://127.0.0.1:8000 and stays up across reboots/logins. (If `pipenv run` misbehaves
   under launchd, point ProgramArguments at `./.venv/bin/uvicorn` directly instead.)
3. **Schedule wakes** so scans run even when the Mac would otherwise sleep:
   ```bash
   sudo deploy/setup_pmset.sh
   ```

**Caveat (be honest about this):** a *sleeping* Mac runs nothing. `pmset` wakes work
reliably only on power; a closed lid on battery usually won't wake. If you need scans
to keep running no matter what, use Option B.

To stop: `launchctl unload -w ~/Library/LaunchAgents/com.dealfinder.plist`.

## Option B — always-on host (most reliable, true 24/7)

Run it on a machine that never sleeps: a Raspberry Pi 5 (16 GB), an old laptop, a mini
PC, or a small VPS. Same code, same commands (`pipenv install --dev` then
`pipenv run start` under systemd or the host's equivalent of launchd).

For the local AI on that host, either:
- run Ollama there (a Pi can run the small Gemma E2B/E4B models, slowly), or
- point `DF_OLLAMA_BASE_URL` at another machine on your network that runs Ollama.

No token cost either way.

## Checking it's working

- `curl http://127.0.0.1:8000/api/health` → `db`, `scheduler_running`, `ai_reachable`, `smtp_configured`.
- Tail the log: `tail -f dealfinder.log`.
- In the UI, a started watch shows its **next run** time.
