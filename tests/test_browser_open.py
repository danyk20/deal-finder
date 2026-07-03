from __future__ import annotations

from deal_finder.notify import browser_open


def test_open_listings_opens_each_url(monkeypatch):
    opened = []
    monkeypatch.setattr(browser_open.webbrowser, "open_new_tab", lambda url: opened.append(url))
    monkeypatch.setattr(browser_open.time, "sleep", lambda s: None)  # keep the test instant

    n = browser_open.open_listings(["https://a", "https://b", "https://c"])
    assert n == 3
    assert opened == ["https://a", "https://b", "https://c"]


def test_open_listings_empty():
    assert browser_open.open_listings([]) == 0


def test_open_listings_one_bad_url_does_not_stop_the_rest(monkeypatch):
    def flaky(url):
        if url == "https://bad":
            raise OSError("no browser found")
        opened.append(url)

    opened = []
    monkeypatch.setattr(browser_open.webbrowser, "open_new_tab", flaky)
    monkeypatch.setattr(browser_open.time, "sleep", lambda s: None)

    n = browser_open.open_listings(["https://a", "https://bad", "https://c"])
    assert n == 2  # only the two that succeeded
    assert opened == ["https://a", "https://c"]
