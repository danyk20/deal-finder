from __future__ import annotations

import pytest

from deal_finder.adapters.base import Listing
from deal_finder.ai import Enrichment
from deal_finder.config import Settings
from deal_finder.notify.telegram import (
    TelegramApiError,
    TelegramMatch,
    TelegramNotConfigured,
    render_caption,
    render_telegram_message,
    send_telegram,
    send_telegram_match,
)


def _match(title="Tesla Model S 75D", desc="Sehr gepflegt", with_photo=True):
    li = Listing(
        marketplace="demo", external_id="1", url="https://x/listing-1",
        title=title, description=desc, price=38900,
        location="Zürich", attributes={"year": 2017, "mileage_km": 95000},
        image_urls=["https://x/photo.jpg"] if with_photo else [],
    )
    en = Enrichment(
        translated_description="Very well maintained.",
        answers={"Condition?": "Looks great"}, ai_used=True,
    )
    return TelegramMatch(listing=li, enrichment=en, questions=["Condition?"])


def test_render_telegram_message_contents():
    text, photo = render_telegram_message(_match())
    assert "Tesla Model S 75D" in text
    assert "https://x/listing-1" in text
    assert "Very well maintained." in text
    assert "Condition?" in text and "Looks great" in text
    assert "38" in text
    assert photo == "https://x/photo.jpg"


def test_render_telegram_message_no_photo():
    _, photo = render_telegram_message(_match(with_photo=False))
    assert photo is None


def test_html_escaping():
    text, _ = render_telegram_message(_match(title="A & B <script>"))
    assert "&amp;" in text
    assert "&lt;script&gt;" in text
    assert "<script>" not in text


def test_render_caption_truncates_body_not_header():
    match = _match(desc="x" * 5000)
    caption = render_caption(match)
    assert len(caption) <= 1024
    assert "Tesla Model S 75D" in caption
    assert "https://x/listing-1" in caption


def test_safe_truncate_never_leaves_dangling_entity():
    match = _match(title="A" * 900, desc="&amp;" * 50)
    caption = render_caption(match)
    assert not caption.rstrip("…").endswith("&")
    assert "&am<" not in caption
    for bad in ("&a", "&am", "&amp", "&amp;a"):
        assert not caption.endswith(bad)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {"ok": True}

    def json(self):
        return self._json


def test_send_telegram_message_payload(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json))
        return FakeResponse()

    monkeypatch.setattr("deal_finder.notify.telegram.httpx.post", fake_post)
    settings = Settings(telegram_bot_token="TOKEN")
    send_telegram(settings, "12345", "hello")
    url, payload = calls[-1]
    assert url == "https://api.telegram.org/botTOKEN/sendMessage"
    assert payload["chat_id"] == "12345"
    assert payload["text"] == "hello"


def test_send_telegram_requires_token():
    with pytest.raises(TelegramNotConfigured):
        send_telegram(Settings(telegram_bot_token=""), "12345", "hi")


def test_send_telegram_requires_chat_id():
    with pytest.raises(TelegramNotConfigured):
        send_telegram(Settings(telegram_bot_token="TOKEN"), "", "hi")


def test_send_telegram_raises_api_error(monkeypatch):
    monkeypatch.setattr(
        "deal_finder.notify.telegram.httpx.post",
        lambda *a, **k: FakeResponse(json_data={"ok": False, "description": "bad chat id"}),
    )
    settings = Settings(telegram_bot_token="TOKEN")
    with pytest.raises(TelegramApiError, match="bad chat id"):
        send_telegram(settings, "12345", "hi")


def test_send_telegram_match_falls_back_to_text_on_photo_failure(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json))
        if "sendPhoto" in url:
            return FakeResponse(json_data={"ok": False, "description": "can't fetch photo"})
        return FakeResponse()

    monkeypatch.setattr("deal_finder.notify.telegram.httpx.post", fake_post)
    settings = Settings(telegram_bot_token="TOKEN")
    send_telegram_match(settings, "12345", _match())

    assert len(calls) == 2
    assert "sendPhoto" in calls[0][0]
    assert "sendMessage" in calls[1][0]
    # The fallback must carry the FULL message text, not the short caption.
    assert "Very well maintained." in calls[1][1]["text"]


def test_send_telegram_match_sends_photo_when_available(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "deal_finder.notify.telegram.httpx.post",
        lambda url, json, timeout: calls.append((url, json)) or FakeResponse(),
    )
    settings = Settings(telegram_bot_token="TOKEN")
    send_telegram_match(settings, "12345", _match())
    assert len(calls) == 1
    assert "sendPhoto" in calls[0][0]


def test_send_telegram_match_no_photo_sends_message_directly(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "deal_finder.notify.telegram.httpx.post",
        lambda url, json, timeout: calls.append((url, json)) or FakeResponse(),
    )
    settings = Settings(telegram_bot_token="TOKEN")
    send_telegram_match(settings, "12345", _match(with_photo=False))
    assert len(calls) == 1
    assert "sendMessage" in calls[0][0]
