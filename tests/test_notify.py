from __future__ import annotations

import pytest

from deal_finder.adapters.base import Listing
from deal_finder.ai import Enrichment
from deal_finder.config import Settings
from deal_finder.models import Watch
from deal_finder.notify import EmailMatch, render_email, send_email
from deal_finder.notify.email import EmailNotConfigured


def _match():
    li = Listing(
        marketplace="demo", external_id="1", url="https://x/listing-1",
        title="Tesla Model S 75D", description="Sehr gepflegt", price=38900,
        location="Zürich", attributes={"year": 2017, "mileage_km": 95000},
    )
    en = Enrichment(
        translated_description="Very well maintained.",
        answers={"Condition?": "Looks great"}, ai_used=True,
    )
    return EmailMatch(listing=li, enrichment=en, questions=["Condition?"])


def test_render_email_contents():
    w = Watch(name="MS watch", search_params={"model": "Model S"})
    subject, html = render_email(w, [_match()])
    assert "Model S" in subject
    assert "https://x/listing-1" in html  # clickable link
    assert "Very well maintained." in html  # translated description
    assert "Condition?" in html and "Looks great" in html  # AI Q&A
    assert "38" in html  # price rendered


class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def test_send_email(monkeypatch):
    import deal_finder.notify.email as em

    FakeSMTP.instances.clear()
    monkeypatch.setattr(em.smtplib, "SMTP", FakeSMTP)
    settings = Settings(
        smtp_host="smtp.test", smtp_port=587, smtp_user="u", smtp_password="p",
        smtp_from="from@x", smtp_starttls=True,
    )
    send_email(settings, "to@x", "Subject", "<b>hi</b>")
    inst = FakeSMTP.instances[-1]
    assert inst.started_tls is True
    assert inst.logged == ("u", "p")
    assert inst.sent[0]["To"] == "to@x"
    assert inst.sent[0]["Subject"] == "Subject"


def test_send_email_requires_host():
    with pytest.raises(EmailNotConfigured):
        send_email(Settings(smtp_host=""), "to@x", "s", "h")


def test_send_email_requires_recipient():
    with pytest.raises(EmailNotConfigured):
        send_email(Settings(smtp_host="smtp.test"), "", "s", "h")
