"""Clear a one-time bot-challenge yourself in a visible browser.

Applies to the adapters that use deal_finder's own shared browser/ session (tutti,
Ricardo) — those may show a "checking your browser" / "I am not a robot" step. Deal
Finder never solves these itself — instead this opens a VISIBLE Chrome on the site's
persistent profile so YOU complete the step once. The cleared session then persists in
the profile, and scheduled scans reuse it. Re-run this if scans start reporting
challenges again.

AutoScout24 (a plain public JSON API) and Facebook (its own dedicated package with its
own login flow — see `python -m deal_finder.browser.fb_login`) don't use this session
and aren't valid targets here.

Run:
    python -m deal_finder.browser.solve tutti
    python -m deal_finder.browser.solve ricardo
    python -m deal_finder.browser.solve https://www.tutti.ch/de/q/autos?query=Tesla%20Model%20S
"""

from __future__ import annotations

import sys

from ..config import get_settings
from .human import dismiss_cookie_banner
from .session import BrowserConfig, BrowserSession

# adapter key -> (profile subdir, a representative warm-up URL)
_TARGETS = {
    "tutti": ("tutti", "https://www.tutti.ch/de/q/autos?query=Tesla%20Model%20S"),
    "ricardo": ("ricardo", "https://www.ricardo.ch/de/s/Tesla%20Model%20S"),
}


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m deal_finder.browser.solve <adapter-key|url>")
        print("  keys:", ", ".join(_TARGETS))
        return
    arg = argv[0]
    profile, url = _TARGETS.get(arg, ("default", arg))

    cfg = BrowserConfig.from_settings(get_settings(), profile=profile)
    cfg.headless = False  # must be visible so you can complete the challenge
    print(f"Opening {url}\n(profile '{profile}', visible Chrome)…")
    with BrowserSession(cfg) as session:
        page = session.playwright_page
        page.goto(url, wait_until="domcontentloaded")
        try:
            dismiss_cookie_banner(page)
        except Exception:  # noqa: BLE001
            pass
        print(
            "\nIf you see a 'checking your browser' or 'I am not a robot' step, complete "
            "it yourself in the window. Then browse a real listing or two so the session "
            "looks used.\n"
        )
        input("When the real results are visible, press Enter to save and close… ")
    print(f"Saved. The '{profile}' profile now carries the cleared session.")


if __name__ == "__main__":
    main()
