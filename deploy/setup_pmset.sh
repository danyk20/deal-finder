#!/usr/bin/env bash
# Schedule the Mac to WAKE so Deal Finder can run scans even when it would be asleep.
#
# A sleeping Mac runs no processes. macOS can wake itself on a schedule via `pmset`.
# The app's own per-watch schedule then runs the actual scan once awake; APScheduler's
# misfire grace window catches a job whose exact time fell during sleep.
#
# This works reliably only while the Mac is plugged into power. Clamshell (lid closed)
# on battery generally will NOT wake. For true 24/7 independence, run Deal Finder on an
# always-on host (Raspberry Pi / mini PC / VPS) instead — see deploy/README_deploy.md.
#
# Usage:
#   sudo deploy/setup_pmset.sh            # wake every day at 07:55, 11:55, 15:55, 19:55
#   sudo pmset -g sched                   # show scheduled wakes
#   sudo pmset repeat cancel              # remove the repeating schedule
#
# Edit the time/days below to match your watches' cadence. `pmset repeat` supports one
# repeating rule; for multiple distinct times use `pmset schedule wake "MM/DD/YYYY HH:MM:SS"`.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo (pmset needs root): sudo $0" >&2
  exit 1
fi

# Wake Monday–Sunday; the app scans shortly after each wake.
# Keep the app's interval a bit shorter than the gap between wakes so a scan is pending.
pmset repeat wakeorpoweron MTWRFSU 07:55:00

echo "Scheduled a daily repeating wake at 07:55. Current schedule:"
pmset -g sched
echo
echo "Tip: keep the Mac on power. To scan multiple times per day, add one-off wakes, e.g.:"
echo "  sudo pmset schedule wake \"\$(date -v+4H '+%m/%d/%Y %H:%M:%S')\""
