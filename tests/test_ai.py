from __future__ import annotations

import httpx
import pytest

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


def _photos(monkeypatch, n):
    fetched = []

    def fake_get(url, **kw):
        fetched.append(url)
        return FakeImageResponse()

    monkeypatch.setattr("deal_finder.ai.dealbreakers.httpx.get", fake_get)
    listing = _listing_with_attributes()
    listing.image_urls = [f"https://x/photo{i}.jpg" for i in range(1, n + 1)]
    return listing, fetched


def _image_parts(message):
    content = message[1]["content"]
    return [] if isinstance(content, str) else [p for p in content if p.get("type") == "image_url"]


def test_check_non_negotiables_decides_from_text_alone_without_photos(monkeypatch):
    """The regression: a listing saying "8 GB RAM" passed "more than 32 GB of RAM", because
    sending all its photos made the model time out (-> fail open) or ignore the text. The
    text is judged first, and photos aren't even downloaded when it settles the question."""
    listing, fetched = _photos(monkeypatch, 5)
    client = StubClient(["FAIL: only 8 GB of RAM"])
    passed, reason = check_non_negotiables(client, listing, "Must have more than 32 GB of RAM")
    assert (passed, reason) == (False, "only 8 GB of RAM")
    assert client.calls == 1 and not _image_parts(client.messages[0]) and fetched == []
    assert "mileage_km: 95000" in client.messages[0][1]["content"]
    assert "Must have more than 32 GB of RAM" in client.messages[0][1]["content"]


def test_check_non_negotiables_text_pass_skips_photos(monkeypatch):
    listing, fetched = _photos(monkeypatch, 5)
    client = StubClient(["PASS"])
    assert check_non_negotiables(client, listing, "must be electric") == (True, None)
    assert client.calls == 1 and fetched == []


def test_check_non_negotiables_unknown_looks_at_photos_in_batches_of_3(monkeypatch):
    """Ollama's OpenAI-compatible endpoint rejects remote image_url values outright, so
    every photo must be fetched and inlined as a base64 data: URI -- not passed through
    as the marketplace's original URL. A few per call: 5 at once confused the model."""
    listing, fetched = _photos(monkeypatch, 7)
    client = StubClient(["UNKNOWN: the colour isn't stated", "UNKNOWN", "FAIL: the car is red"])
    assert check_non_negotiables(client, listing, "must be green") == (False, "the car is red")
    assert [len(_image_parts(m)) for m in client.messages] == [0, 3, 3]  # stopped before the 7th
    assert fetched == [f"https://x/photo{i}.jpg" for i in range(1, 7)]
    photo_call = client.messages[1][1]["content"]
    assert _image_parts(client.messages[1])[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    text = next(p for p in photo_call if p.get("type") == "text")["text"]
    assert "must be green" in text and "NOT SETTLED BY THE TEXT: the colour isn't stated" in text


def test_check_non_negotiables_photos_can_confirm(monkeypatch):
    listing, _ = _photos(monkeypatch, 4)
    client = StubClient(["UNKNOWN: colour", "PASS"])
    assert check_non_negotiables(client, listing, "must be green") == (True, None)
    assert client.calls == 2


def test_check_non_negotiables_unknown_everywhere_gets_the_benefit_of_the_doubt(monkeypatch):
    listing, _ = _photos(monkeypatch, 4)
    client = StubClient(["UNKNOWN: colour", "UNKNOWN", "UNKNOWN"])
    assert check_non_negotiables(client, listing, "must be green") == (True, None)
    assert client.calls == 3


def test_check_non_negotiables_unknown_without_photos_passes():
    client = StubClient(["UNKNOWN: colour"])
    assert check_non_negotiables(client, _listing(), "must be green") == (True, None)
    assert client.calls == 1


def test_check_non_negotiables_skips_unfetchable_photo(monkeypatch):
    """A broken image link must never abort the whole check -- just proceed without it."""
    monkeypatch.setattr(
        "deal_finder.ai.dealbreakers.httpx.get",
        lambda url, **kw: (_ for _ in ()).throw(httpx.HTTPError("boom")),
    )
    listing = _listing_with_attributes()
    listing.image_urls = ["https://x/broken.jpg"]
    client = StubClient(["UNKNOWN: colour"])
    passed, reason = check_non_negotiables(client, listing, "must be green")
    assert passed is True
    assert client.calls == 1  # no photo call with zero photos


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("**FAIL**: only 8 GB of RAM", (False, "only 8 GB of RAM")),
        ("Verdict: FAIL: only 8 GB of RAM", (False, "only 8 GB of RAM")),
        ("fail - only 8 GB", (False, "- only 8 GB")),
        ("**PASS**", (True, None)),
        ("", (True, None)),  # unreadable -> logged, fails open (not a silent PASS verdict)
    ],
)
def test_check_non_negotiables_reads_verdict_robustly(answer, expected):
    assert check_non_negotiables(StubClient([answer]), _listing(), "more than 32 GB of RAM") == expected


def test_check_non_negotiables_asks_without_reasoning():
    seen = {}

    class Client(StubClient):
        def chat(self, messages, **kwargs):
            seen.update(kwargs)
            return super().chat(messages, **kwargs)

    check_non_negotiables(Client(["PASS"]), _listing(), "must be green")
    assert seen["reasoning_effort"] == "none"


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
