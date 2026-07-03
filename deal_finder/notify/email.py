"""Render and send match notification emails (SMTP)."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..adapters.base import Listing
from ..ai import Enrichment
from ..config import Settings
from ..models import Watch

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


@dataclass
class EmailMatch:
    listing: Listing
    enrichment: Enrichment
    questions: list[str]


class EmailNotConfigured(Exception):
    pass


def render_email(watch: Watch, matches: list[EmailMatch]) -> tuple[str, str]:
    """Return (subject, html_body)."""
    item = (watch.search_params or {}).get("model") or watch.name
    n = len(matches)
    subject = f"Deal Finder: {n} new {item} match{'' if n == 1 else 'es'}"
    html = _env.get_template("match_email.html").render(watch=watch, matches=matches)
    return subject, html


def send_email(settings: Settings, to_addr: str, subject: str, html_body: str) -> None:
    """Send one HTML email. Raises on misconfiguration or SMTP failure."""
    if not settings.smtp_host:
        raise EmailNotConfigured("SMTP host is not configured (set DF_SMTP_* or Settings).")
    if not to_addr:
        raise EmailNotConfigured("No recipient email address set for this watch.")

    sender = settings.smtp_from or settings.smtp_user or to_addr
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg.set_content("This message contains HTML. Please view it in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        if settings.smtp_starttls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
