from __future__ import annotations

import httpx

from deal_finder.adapters.base import Listing
from deal_finder.ai import enrich_listing
from deal_finder.ai.client import AiUnavailable
from deal_finder.ai.dealbreakers import check_non_negotiables
from deal_finder.ai.questions import answer_questions
from deal_finder.ai.translate import translate_text
from deal_finder.config import Settings


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages: list[list[dict]] = []

    def chat(self, messages, **kwargs):
        self.calls += 1
        self.messages.append(messages)
        return self.responses.pop(0)


class RaisingClient:
    def chat(self, messages, **kwargs):
        raise AiUnavailable("server down")


def _listing():
    return Listing(marketplace="demo", external_id="1", url="http://x", title="t",
                   description="Sehr gepflegt, Abholung in Zürich.")


def _listing_with_attributes():
    return Listing(
        marketplace="demo", external_id="1", url="http://x", title="Tesla Model S",
        description="Sehr gepflegt.",  # mileage/fuel deliberately NOT mentioned here
        price=38900, location="Zürich",
        attributes={"year": 2017, "mileage_km": 95000, "fuel": "electric"},
    )


def test_translate_skips_when_source_matches_target():
    client = StubClient([])
    assert translate_text(client, "Already English", source_language="en") == "Already English"
    assert client.calls == 0


def test_translate_calls_model():
    client = StubClient(["Very well maintained."])
    assert translate_text(client, "Sehr gepflegt.") == "Very well maintained."


def test_translate_to_custom_target_language():
    client = StubClient(["Très bien entretenue."])
    out = translate_text(client, "Very well maintained.", target_language="French")
    assert out == "Très bien entretenue."
    system_prompt = client.messages[0][0]["content"]
    assert "French" in system_prompt


def test_translate_skips_when_source_matches_custom_target():
    client = StubClient([])
    assert translate_text(client, "Sehr gepflegt.", source_language="de", target_language="German") == "Sehr gepflegt."
    assert client.calls == 0


def test_questions_one_call_per_question():
    client = StubClient(["Yes, looks great", "not stated"])
    out = answer_questions(client, "desc", ["Condition?", "Pickup?"])
    assert out["Condition?"] == "Yes, looks great"
    assert out["Pickup?"] == "not stated"
    assert client.calls == 2


def test_questions_reports_progress():
    client = StubClient(["Good", "Fine"])
    seen = []
    answer_questions(
        client, "desc", ["Condition?", "Pickup?"],
        on_progress=lambda i, total, q: seen.append((i, total, q)),
    )
    assert seen == [(1, 2, "Condition?"), (2, 2, "Pickup?")]


def test_enrich_disabled():
    en = enrich_listing(Settings(ai_enabled=False), _listing(), ["Q?"])
    assert en.ai_used is False
    assert "disabled" in en.note


def test_enrich_graceful_when_model_down():
    en = enrich_listing(Settings(smtp_host="x"), _listing(), ["Q?"], client=RaisingClient())
    assert en.ai_used is False
    assert "unavailable" in en.note
    assert en.answers["Q?"] == "not stated"


def test_enrich_success():
    client = StubClient(["English translation.", "Perfect"])
    en = enrich_listing(Settings(smtp_host="x"), _listing(), ["Condition?"], client=client)
    assert en.ai_used is True
    assert en.translated_description == "English translation."
    assert en.answers["Condition?"] == "Perfect"


def test_listing_as_key_value_text_includes_structured_fields():
    text = _listing_with_attributes().as_key_value_text
    assert "mileage_km: 95000" in text
    assert "fuel: electric" in text
    assert "year: 2017" in text
    assert "price: 38900" in text
    assert "location: Zürich" in text


def test_enrich_questions_can_see_fields_missing_from_description():
    """The mileage/fuel facts live only in `attributes`, never in `description` -- make
    sure the question call's prompt actually contains them, not just the description."""
    client = StubClient(["English translation.", "95000 km, electric"])
    en = enrich_listing(
        Settings(smtp_host="x"), _listing_with_attributes(), ["What's the mileage and fuel type?"],
        client=client,
    )
    question_call = client.messages[1]
    user_content = question_call[1]["content"]
    assert "mileage_km: 95000" in user_content
    assert "fuel: electric" in user_content
    assert en.answers["What's the mileage and fuel type?"] == "95000 km, electric"


def test_check_non_negotiables_blank_requirement_skips_call():
    client = StubClient([])
    passed, reason = check_non_negotiables(client, _listing(), "   ")
    assert passed is True and reason is None
    assert client.calls == 0


def test_check_non_negotiables_pass():
    client = StubClient(["PASS"])
    passed, reason = check_non_negotiables(client, _listing(), "must be green")
    assert passed is True and reason is None


def test_check_non_negotiables_fail_extracts_reason():
    client = StubClient(["FAIL: the car is red, not green"])
    passed, reason = check_non_negotiables(client, _listing(), "must be green")
    assert passed is False
    assert reason == "the car is red, not green"


def test_check_non_negotiables_fails_open_on_ai_unavailable():
    passed, reason = check_non_negotiables(RaisingClient(), _listing(), "must be green")
    assert passed is True and reason is None


class FakeImageResponse:
    def __init__(self, content=b"\xff\xd8\xfffakejpegbytes", content_type="image/jpeg", status_code=200):
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def test_check_non_negotiables_includes_photos_as_base64_data_uris(monkeypatch):
    """Ollama's OpenAI-compatible endpoint rejects remote image_url values outright, so
    every photo must be fetched and inlined as a base64 data: URI -- not passed through
    as the marketplace's original URL."""
    monkeypatch.setattr(
        "deal_finder.ai.dealbreakers.httpx.get",
        lambda url, **kw: FakeImageResponse(),
    )
    listing = _listing_with_attributes()
    listing.image_urls = ["https://x/photo1.jpg", "https://x/photo2.jpg"]
    client = StubClient(["PASS"])
    check_non_negotiables(client, listing, "must be green")
    content = client.messages[0][1]["content"]  # user message content
    image_parts = [p for p in content if p.get("type") == "image_url"]
    assert len(image_parts) == 2
    assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    text_part = next(p for p in content if p.get("type") == "text")
    assert "mileage_km: 95000" in text_part["text"]
    assert "must be green" in text_part["text"]


def test_check_non_negotiables_skips_unfetchable_photo(monkeypatch):
    """A broken image link must never abort the whole check -- just proceed without it."""
    monkeypatch.setattr(
        "deal_finder.ai.dealbreakers.httpx.get",
        lambda url, **kw: (_ for _ in ()).throw(httpx.HTTPError("boom")),
    )
    listing = _listing_with_attributes()
    listing.image_urls = ["https://x/broken.jpg"]
    client = StubClient(["PASS"])
    passed, reason = check_non_negotiables(client, listing, "must be green")
    assert passed is True
    content = client.messages[0][1]["content"]
    assert not [p for p in content if p.get("type") == "image_url"]


def test_enrich_reports_progress():
    client = StubClient(["English translation.", "Perfect"])
    seen = []
    enrich_listing(
        Settings(smtp_host="x"), _listing(), ["Condition?"], client=client,
        on_progress=seen.append,
    )
    assert seen[0] == "translating description to English"
    assert "Condition?" in seen[1]


def test_enrich_uses_configured_target_language():
    client = StubClient(["Sehr gut.", "Gut"])
    en = enrich_listing(
        Settings(smtp_host="x", ai_translate_to="German"), _listing(), ["Condition?"], client=client,
    )
    assert en.translated_description == "Sehr gut."
    system_prompt = client.messages[0][0]["content"]
    assert "German" in system_prompt
