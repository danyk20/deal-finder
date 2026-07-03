"""One-time manual Facebook login for the `facebook-marketplace-scraper` package the
Facebook adapter uses (see deal_finder/adapters/facebook.py). Opens a visible Chrome so
you can log in yourself (handles 2FA, and Marketplace's data-use consent screen if it
appears); the session then persists in that package's own browser profile and later
scans reuse it automatically — no password is stored by Deal Finder.

Note: the session is stored inside the installed package's own directory (see
fb_scraper.browser.PROFILE_DIR), not in deal_finder's ~/.deal_finder/profiles/ — a
`pipenv sync` / dependency reinstall wipes it, and this needs to be run again after that.

Run:  python -m deal_finder.browser.fb_login
"""

from __future__ import annotations


def main() -> None:
    try:
        from fb_scraper.browser import FacebookSession
    except ImportError:
        print(
            "'facebook-marketplace-scraper' isn't installed; run:\n"
            "  pipenv install --categories facebook\n"
            "(or: pip install facebook-marketplace-scraper)"
        )
        return

    print("Opening Chrome for Facebook login…")
    with FacebookSession(headless=False) as context:
        page = context.new_page()
        page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
        # FacebookSession itself detects whether this profile is already logged in and,
        # if not, prints its own prompt and waits for you here — nothing more to do.
        page.close()
    print("Done. The Facebook session is saved for later scans to reuse.")


if __name__ == "__main__":
    main()
