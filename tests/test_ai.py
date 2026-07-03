from __future__ import annotations

from deal_finder.adapters.base import Listing
from deal_finder.ai import enrich_listing
from deal_finder.ai.client import AiUnavailable
from deal_finder.ai.questions import answer_questions
from deal_finder.ai.translate import translate_to_english
from deal_finder.config import Settings


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class RaisingClient:
    def chat(self, messages, **kwargs):
        raise AiUnavailable("server down")


def _listing():
    return Listing(marketplace="demo", external_id="1", url="http://x", title="t",
                   description="Sehr gepflegt, Abholung in Zürich.")


def test_translate_skips_english():
    client = StubClient([])
    assert translate_to_english(client, "Already English", source_language="en") == "Already English"
    assert client.calls == 0


def test_translate_calls_model():
    client = StubClient(["Very well maintained."])
    assert translate_to_english(client, "Sehr gepflegt.") == "Very well maintained."


def test_questions_parse_json():
    client = StubClient(['{"1": "Yes, looks great", "2": "not stated"}'])
    out = answer_questions(client, "desc", ["Condition?", "Pickup?"])
    assert out["Condition?"] == "Yes, looks great"
    assert out["Pickup?"] == "not stated"


def test_questions_tolerate_code_fences():
    client = StubClient(['```json\n{"1": "Good"}\n```'])
    out = answer_questions(client, "desc", ["Condition?"])
    assert out["Condition?"] == "Good"


def test_questions_unparseable_falls_back():
    client = StubClient(["sorry, I cannot help"])
    out = answer_questions(client, "desc", ["Condition?"])
    assert "could not be parsed" in out["Condition?"]


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
    client = StubClient(["English translation.", '{"1": "Perfect"}'])
    en = enrich_listing(Settings(smtp_host="x"), _listing(), ["Condition?"], client=client)
    assert en.ai_used is True
    assert en.translated_description == "English translation."
    assert en.answers["Condition?"] == "Perfect"
