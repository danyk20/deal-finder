"""Curated list of major world languages offered as AI translation targets.

Gemma's model card only advertises an aggregate "out-of-the-box support for 35+
languages, pre-trained on 140+" with no enumerated list published anywhere (Google AI
model card, Hugging Face, and the technical report all stop at the aggregate count) --
so this is a practical curated set of major world languages a multilingual model like
Gemma's family is expected to handle well, each paired with a flag for the Settings UI.
Free-text values are still accepted by :func:`deal_finder.ai.translate.translate_text`;
this list only bounds what the Settings dropdown offers.
"""

from __future__ import annotations

SUPPORTED_LANGUAGES: list[tuple[str, str]] = [
    ("English", "🇬🇧"),
    ("German", "🇩🇪"),
    ("French", "🇫🇷"),
    ("Italian", "🇮🇹"),
    ("Spanish", "🇪🇸"),
    ("Portuguese", "🇵🇹"),
    ("Dutch", "🇳🇱"),
    ("Polish", "🇵🇱"),
    ("Russian", "🇷🇺"),
    ("Ukrainian", "🇺🇦"),
    ("Czech", "🇨🇿"),
    ("Slovak", "🇸🇰"),
    ("Hungarian", "🇭🇺"),
    ("Romanian", "🇷🇴"),
    ("Bulgarian", "🇧🇬"),
    ("Greek", "🇬🇷"),
    ("Swedish", "🇸🇪"),
    ("Norwegian", "🇳🇴"),
    ("Danish", "🇩🇰"),
    ("Finnish", "🇫🇮"),
    ("Croatian", "🇭🇷"),
    ("Serbian", "🇷🇸"),
    ("Turkish", "🇹🇷"),
    ("Arabic", "🇸🇦"),
    ("Hebrew", "🇮🇱"),
    ("Hindi", "🇮🇳"),
    ("Bengali", "🇧🇩"),
    ("Chinese", "🇨🇳"),
    ("Japanese", "🇯🇵"),
    ("Korean", "🇰🇷"),
    ("Vietnamese", "🇻🇳"),
    ("Thai", "🇹🇭"),
    ("Indonesian", "🇮🇩"),
    ("Malay", "🇲🇾"),
    ("Swahili", "🇰🇪"),
]
