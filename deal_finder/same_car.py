"""Is it the same used car? A dealer's ads of one car on two portals (AutoScout24 and
carmarket.ch, say -- the latter reached through AutoUncle) share no ID or link, and their
titles differ ("TESLA Model S 100 D" / "Tesla Model S 100 D 2017"). What they share is the
car's own data: year, exact mileage, price, and whatever else both ads state.

An ad is only ever dropped as a copy when that's certain -- a very similar car from the
same dealer (same model, same showroom) must still be shown:

- year, mileage and price must be equal;
- every other fact both ads state must agree -- postcode, seller, first registration,
  power, fuel, gearbox, colour, VIN; one that differs means two cars;
- at least ``MIN_AGREEING`` of them must agree (or the VIN), so a pair with little data
  beyond year, mileage and price is kept as two -- compare() decides this much;
- and the photos must be the same ones (see same_photos): at least two, and at least half
  of those compared. Photos missing, or that can't be loaded, keep the two ads.

Publication dates only ever count for it (within ``_PUBLISHED_DAYS``): a dealer may add a
portal weeks later, and AutoUncle's date is when it first saw the ad. Body type and doors
aren't compared at all: the portals classify them differently (AutoScout24 "saloon",
AutoUncle "Schrägheck" for the same Tesla Model S).
"""

from __future__ import annotations

import calendar
import io
import json
import logging
import re
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from typing import Any

import httpx
from PIL import Image, ImageOps

from .adapters.base import Listing

log = logging.getLogger("deal_finder.same_car")

MIN_AGREEING = 3
# Below this a car is new or a demonstrator, and a dealer may well have several identical ones.
_MIN_KM = 1000
_PUBLISHED_DAYS = 3

# Photos: a 256-bit difference hash per photo -- the same photo stays the same through the
# resizing and re-encoding each portal does. Calibrated on real ads (2026-11): the same
# photos on AutoScout24 and (via AutoUncle) carmarket.ch differ in 1-5 bits; the same
# dealer's other black Tesla Model S, shot in the same showroom, in 83 or more.
_HASH_SIZE = 16
_SAME_PHOTO_BITS = 24
_MAX_PHOTOS = 6  # per ad: AutoUncle has 4, the first ones of the ad on its portal
_MAX_PHOTO_BYTES = 15 * 1024 * 1024  # AutoScout24 serves originals, up to 4032x3024

_POSTCODE_RE = re.compile(r"\b(\d{4})\b")  # Swiss: the first 4-digit number of a location
_YEAR_MONTH_RE = re.compile(r"\b(\d{4})-(\d{1,2})\b")  # "2017-11", "2017-11-01"
_MONTH_YEAR_RE = re.compile(r"\b(\d{1,2})[./](\d{4})\b")  # "11.2017", "01.11.2017", "11/2017"
_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}

# Canonical values, matched against words of the listing's own text (de/fr/it/en).
_FUELS = {
    "hydrogen": ("wasserstoff", "hydrogen", "hydrogène", "idrogeno"),
    "gas": ("erdgas", "cng", "lpg", "autogas", "gpl", "metano"),
    "electric": ("elektro", "electric", "électrique", "elettrico", "elettrica", "bev"),
    "petrol": ("benzin", "petrol", "gasoline", "essence", "benzina", "bleifrei"),
    "diesel": ("diesel",),
}
_COLOURS = {
    "black": ("schwarz", "black", "noir", "nero"),
    "white": ("weiss", "white", "blanc", "bianco"),
    # one group: the portals don't agree where grey ends and silver starts
    "grey": ("grau", "grey", "gray", "gris", "grigio", "anthrazit", "anthracite", "antracite",
             "silber", "silver", "argent", "argento", "titan"),
    "blue": ("blau", "blue", "bleu", "blu", "azzurro"),
    "red": ("rot", "red", "rouge", "rosso", "bordeaux"),
    "green": ("grün", "gruen", "green", "vert", "verde"),
    "brown": ("braun", "brown", "brun", "marrone"),
    "beige": ("beige", "creme", "crème"),
    "yellow": ("gelb", "yellow", "jaune", "giallo"),
    "orange": ("orange", "arancione"),
    "gold": ("gold", "oro"),
    "violet": ("violett", "lila", "purple", "violet", "viola"),
}
_LEGAL_FORMS = {"ag", "gmbh", "sa", "sarl", "sàrl", "sagl", "ltd", "kg", "co", "und", "et", "the"}


@dataclass(frozen=True)
class CarFacts:
    """What one ad says about its car, in comparable form (None: the ad doesn't say)."""

    year: int
    mileage_km: int
    price: int
    postcode: str | None = None
    seller: str | None = None
    registration: str | None = None  # first registration, "2017-11"
    horsepower: int | None = None
    power_kw: int | None = None
    fuel: str | None = None  # a key of _FUELS, or "hybrid"
    gearbox: str | None = None  # "automatic" / "manual"
    colour: str | None = None  # a key of _COLOURS
    vin: str | None = None
    published: date | None = None
    photos: tuple[str, ...] = ()  # the ad's first photo links, compared by same_photos

    def to_json(self) -> str:
        """As stored in SeenListing.car_key."""
        data = {k: v for k, v in asdict(self).items() if v not in (None, ())}
        if self.published:
            data["published"] = self.published.isoformat()
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> CarFacts | None:
        try:
            data = json.loads(text)
            data = {f.name: data[f.name] for f in fields(cls) if f.name in data}
            if data.get("published"):
                data["published"] = date.fromisoformat(data["published"])
            data["photos"] = tuple(data.get("photos") or ())
            return cls(**data)
        except (ValueError, TypeError, KeyError):  # "" (nothing stored), or malformed
            return None


def car_facts(listing: Listing) -> CarFacts | None:
    """The listing's CarFacts, or None unless it states a year, a mileage of at least
    _MIN_KM and a price -- every adapter maps those to ``year``/``mileage_km``."""
    a = listing.attributes or {}
    try:
        year, mileage_km, price = int(a.get("year")), int(a.get("mileage_km")), int(listing.price)
    except (TypeError, ValueError):  # missing (None), or not a plain number
        return None
    if mileage_km < _MIN_KM or price <= 0 or not 1900 <= year <= 2100:
        return None
    postcode = _POSTCODE_RE.search(listing.location or "")
    return CarFacts(
        year=year,
        mileage_km=mileage_km,
        price=price,
        postcode=postcode.group(1) if postcode else None,
        seller=_text(a.get("seller_name") or a.get("dealer_name")),
        registration=_registration(a, year),
        horsepower=_int(a.get("horsepower")),
        power_kw=_int(a.get("power_kw")),
        fuel=_fuel(a.get("fuel")),
        gearbox=_gearbox(a.get("transmission")),
        colour=_colour(a.get("body_color") or a.get("color")),
        vin=_vin(a.get("vin")),
        published=_date(listing.posted_at) or _date(a.get("first_seen_at")),
        photos=tuple(listing.image_urls[:_MAX_PHOTOS]),
    )


def compare(a: CarFacts, b: CarFacts) -> tuple[bool, list[str]]:
    """(same car by its facts?, the facts that agree -- or, for two cars, the ones that
    differ). Not the photos: same_photos checks those, once this says yes."""
    if (a.year, a.mileage_km, a.price) != (b.year, b.mileage_km, b.price):
        return False, []
    agree, differ = [], []
    for name, verdict in (
        ("VIN", _equal(a.vin, b.vin)),
        ("postcode", _equal(a.postcode, b.postcode)),
        ("seller", _same_seller(a.seller, b.seller)),
        ("first registration", _equal(a.registration, b.registration)),
        ("power", _same_power(a, b)),
        ("fuel", _equal(a.fuel, b.fuel)),
        ("gearbox", _equal(a.gearbox, b.gearbox)),
        ("colour", _equal(a.colour, b.colour)),
        ("publication date", _published_together(a.published, b.published)),
    ):
        if verdict is True:
            agree.append(name)
        elif verdict is False:
            differ.append(name)
    if differ:
        return False, differ
    return ("VIN" in agree or len(agree) >= MIN_AGREEING), agree


def same_photos(a: CarFacts, b: CarFacts, fingerprints: dict[str, int | None]) -> bool:
    """Whether the two ads show the same photos: at least two of them, and at least half of
    the photos of the ad with fewer (so a dealer's banner photo in every ad of theirs can't
    do it alone). Downloads photos (``fingerprints`` caches them per link), the larger ad's
    only until enough have matched. No photos, or too few loaded: not the same."""
    small, large = sorted((list(a.photos), list(b.photos)), key=len)
    unmatched = [fp for url in small if (fp := _fingerprint(url, fingerprints)) is not None]
    needed = max(2, (len(unmatched) + 1) // 2)
    if len(unmatched) < needed:
        return False
    matched = 0
    for url in large:
        fp = _fingerprint(url, fingerprints)
        if fp is None:
            continue
        same = next((x for x in unmatched if bin(x ^ fp).count("1") <= _SAME_PHOTO_BITS), None)
        if same is not None:
            unmatched.remove(same)
            matched += 1
            if matched >= needed:
                return True
    return False


def fingerprint(data: bytes) -> int | None:
    """A photo's 256-bit difference hash: for each of 16 rows of a 17x16 greyscale copy,
    whether each pixel is darker than its right neighbour. None if it isn't an image."""
    try:
        image = Image.open(io.BytesIO(data))
        image.draft("L", (_HASH_SIZE * 8, _HASH_SIZE * 8))  # JPEG: decode at a fraction of the size
        image = ImageOps.exif_transpose(image).convert("L")
        pixels = image.resize((_HASH_SIZE + 1, _HASH_SIZE), Image.Resampling.LANCZOS).tobytes()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    width, bits = _HASH_SIZE + 1, 0
    for row in range(_HASH_SIZE):
        for col in range(_HASH_SIZE):
            bits = (bits << 1) | (pixels[row * width + col] < pixels[row * width + col + 1])
    return bits


def _fingerprint(url: str, cache: dict[str, int | None]) -> int | None:
    if url not in cache:
        cache[url] = _download_fingerprint(url)
    return cache[url]


def _download_fingerprint(url: str) -> int | None:
    try:
        with httpx.stream("GET", url, timeout=15.0, follow_redirects=True) as resp:
            resp.raise_for_status()
            data = bytearray()
            for chunk in resp.iter_bytes():
                data += chunk
                if len(data) > _MAX_PHOTO_BYTES:
                    return None
    except httpx.HTTPError as exc:
        log.info("couldn't load photo %s: %s", url, exc)
        return None
    return fingerprint(bytes(data))


# --- per-fact verdicts: True agree, False differ, None can't tell ---


def _equal(x, y) -> bool | None:
    return None if x is None or y is None else x == y


def _same_seller(x: str | None, y: str | None) -> bool | None:
    """"Auto-Center Ebikon AG" = "Auto Center Ebikon"; no shared word at all = two sellers;
    a word or so in common ("ACE Ebikon") can't tell."""
    if not x or not y:
        return None
    wx, wy = _seller_words(x), _seller_words(y)
    if not wx or not wy:
        return None
    shared = wx & wy
    if not shared:
        return False
    if shared in (wx, wy) or len(shared) / len(wx | wy) >= 0.5:
        return True
    return None


def _same_power(a: CarFacts, b: CarFacts) -> bool | None:
    """Within 2% (min. 2): portals round a kW figure to PS differently."""
    pairs = ((a.horsepower, b.horsepower, 1.0), (a.power_kw, b.power_kw, 1.0),
             (a.horsepower, b.power_kw, 1.36), (a.power_kw, b.horsepower, 1 / 1.36))
    for x, y, factor in pairs:
        if x and y:
            y = y * factor
            return abs(x - y) <= max(2, 0.02 * max(x, y))
    return None


def _published_together(x: date | None, y: date | None) -> bool | None:
    if x is None or y is None:
        return None
    return True if abs((x - y).days) <= _PUBLISHED_DAYS else None


# --- normalising one ad's values ---


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _words(value: Any) -> list[str]:
    return re.findall(r"[^\W\d_]+", str(value).lower().replace("ß", "ss")) if value else []


def _registration(a: dict, year: int) -> str | None:
    for key in ("first_registration", "firstRegistration"):
        text = _text(str(a[key])) if a.get(key) else None
        if not text:
            continue
        if m := _YEAR_MONTH_RE.search(text):
            y, month = int(m.group(1)), int(m.group(2))
        elif m := _MONTH_YEAR_RE.search(text):
            month, y = int(m.group(1)), int(m.group(2))
        else:
            continue
        if 1 <= month <= 12:
            return f"{y}-{month:02d}"
    month = _MONTHS.get(str(a.get("registration_month") or "").strip().lower())  # AutoScout24: "November"
    return f"{year}-{month:02d}" if month else None


def _fuel(value: Any) -> str | None:
    words = _words(value)
    if any(w.startswith(("hybrid", "ibrid")) or w in ("hev", "mhev", "phev") for w in words):
        return "hybrid"
    found = [name for name, names in _FUELS.items() if any(w in names for w in words)]
    if {"electric", "petrol"} <= set(found) or {"electric", "diesel"} <= set(found):
        return "hybrid"  # "Elektro/Benzin"
    return found[0] if found else None


def _gearbox(value: Any) -> str | None:
    words = _words(value)
    if any(w.startswith(("auto", "halbauto", "semi", "dsg", "cvt", "stufenlos")) for w in words):
        return "automatic"
    if any(w.startswith(("manu", "schalt", "handschalt", "mécani", "mecani", "meccani")) for w in words):
        return "manual"
    return None


def _colour(value: Any) -> str | None:
    """The first base colour named: "Schwarz metallic" / "dunkelblau" (German compounds end
    in it) / "schwarz / beige" (Autolina: outside / inside)."""
    for word in _words(value):
        for name, names in _COLOURS.items():
            if word in names or (len(word) > 5 and word.endswith(tuple(n for n in names if len(n) >= 3))):
                return name
    return None


def _vin(value: Any) -> str | None:
    vin = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return vin if len(vin) == 17 else None  # a full VIN only: never a partly hidden one


def _seller_words(name: str) -> set[str]:
    return {w for w in _words(name) if len(w) >= 3 and w not in _LEGAL_FORMS}


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None
