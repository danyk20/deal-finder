"""Resilient extraction primitives, ordered by robustness:
   1) captured network JSON (handled by the adapter via PageView.json_for),
   2) embedded JSON (__NEXT_DATA__, JSON-LD, script[type=application/json] by __typename),
   3) selectolax DOM fallback.
Plus normalization helpers (price/mileage/year). All pure functions — fixture-testable."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

# --- Embedded JSON ---------------------------------------------------------


def find_json_ld(html: str) -> list[dict]:
    out: list[dict] = []
    for node in HTMLParser(html or "").css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            # schema.org's standard way to bundle multiple related entities (WebPage,
            # Organization, Product, Offer, ...) under one script tag -- confirmed live
            # on Ricardo.ch's current detail pages. Unpack it instead of treating the
            # wrapper object itself as a single (typeless, useless) node.
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                out.extend(item["@graph"])
            else:
                out.append(item)
    return [d for d in out if isinstance(d, dict)]


def find_next_data(html: str) -> dict | None:
    node = HTMLParser(html or "").css_first("script#__NEXT_DATA__")
    if node is None:
        return None
    try:
        return json.loads(node.text() or "")
    except ValueError:
        return None


def get_path(obj: Any, path: str) -> Any:
    """Dotted-path lookup into nested dicts/lists: get_path(d, "props.pageProps.listings").
    Integer segments index lists. Returns None if any step is missing."""
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return None
    return cur


def walk_dicts(obj: Any, pred: Callable[[dict], bool], _out: list[dict] | None = None) -> list[dict]:
    """Recursively collect dicts for which ``pred(d)`` is True — a generic sibling of
    walk_by_typename, for embedded JSON without __typename (e.g. Next.js apps)."""
    out = _out if _out is not None else []
    if isinstance(obj, dict):
        if pred(obj):
            out.append(obj)
        for v in obj.values():
            walk_dicts(v, pred, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_dicts(v, pred, out)
    return out


def walk_by_typename(obj: Any, typenames: set[str], _out: list[dict] | None = None) -> list[dict]:
    """Recursively collect dicts whose __typename is in ``typenames`` (Facebook/Relay style)."""
    out = _out if _out is not None else []
    if isinstance(obj, dict):
        if obj.get("__typename") in typenames:
            out.append(obj)
        for v in obj.values():
            walk_by_typename(v, typenames, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_by_typename(v, typenames, out)
    return out


def json_scripts(html: str) -> list[Any]:
    """Every <script type="application/json"> blob, parsed (Facebook embeds data this way)."""
    out: list[Any] = []
    for node in HTMLParser(html or "").css('script[type="application/json"]'):
        try:
            out.append(json.loads(node.text() or ""))
        except ValueError:
            continue
    return out


# --- DOM fallback ----------------------------------------------------------


def dom_text(html: str, css: str) -> str | None:
    node = HTMLParser(html or "").css_first(css)
    if node is None:
        return None
    txt = node.text(strip=True)
    return txt or None


def dom_all(html: str, css: str) -> list:
    return HTMLParser(html or "").css(css)


def dom_attr(html: str, css: str, attr: str) -> str | None:
    node = HTMLParser(html or "").css_first(css)
    return node.attributes.get(attr) if node is not None else None


def meta_content(html: str, *keys: str) -> str | None:
    """First matching <meta property=key> or <meta name=key> content (e.g. og:title).
    OpenGraph/meta tags are far more stable across redesigns than CSS classes."""
    tree = HTMLParser(html or "")
    for key in keys:
        for sel in (f'meta[property="{key}"]', f'meta[name="{key}"]', f'meta[itemprop="{key}"]'):
            node = tree.css_first(sel)
            if node is not None:
                val = node.attributes.get("content")
                if val:
                    return val
    return None


def og_images(html: str) -> list[str]:
    tree = HTMLParser(html or "")
    urls = []
    for node in tree.css('meta[property="og:image"]'):
        c = node.attributes.get("content")
        if c:
            urls.append(c)
    return urls


# --- Normalization ---------------------------------------------------------

_NUM_RE = re.compile(r"\d[\d'’  .,]*\d|\d")


def _digits_only(s: str) -> int | None:
    m = _NUM_RE.search(s)
    if not m:
        return None
    cleaned = re.sub(r"[^\d]", "", m.group(0))  # drop apostrophes/dots/commas/spaces
    return int(cleaned) if cleaned else None


def parse_price(raw: Any) -> tuple[float | None, str]:
    """Return (amount, currency). Handles numbers, {'amount': n}, and strings like
    "CHF 38'900.-", "Fr. 27500", "€ 45.900"."""
    if raw is None:
        return None, "CHF"
    if isinstance(raw, dict):
        raw = raw.get("amount", raw.get("value", raw.get("amountInCents")))
        if isinstance(raw, (int, float)) and raw and raw % 100 == 0 and raw > 1_000_000:
            raw = raw / 100  # cents heuristic
    if isinstance(raw, (int, float)):
        return float(raw), "CHF"
    s = str(raw)
    currency = "EUR" if ("€" in s or "EUR" in s.upper()) else "CHF"
    n = _digits_only(s)
    return (float(n) if n is not None else None), currency


def parse_int_km(*texts: str) -> int | None:
    for t in texts:
        if not t:
            continue
        m = re.search(r"([\d'’  .]+)\s*km", t, re.IGNORECASE)
        if m:
            digits = re.sub(r"[^\d]", "", m.group(1))
            if digits:
                return int(digits)
    return None


def parse_year(*texts: str) -> int | None:
    """First plausible model/registration year (1980..2035) found across the texts."""
    for t in texts:
        if not t:
            continue
        for m in re.finditer(r"\b(19[89]\d|20[0-3]\d)\b", t):
            year = int(m.group(1))
            if 1980 <= year <= 2035:
                return year
    return None


# --- Higher-level: search cards + car detail fields ------------------------


def card_links(html: str, id_regex: re.Pattern, base_url: str) -> list[tuple[str, str]]:
    """Return unique (external_id, absolute_url) pairs for anchors matching ``id_regex``.
    Robust across redesigns because it keys off the URL pattern, not CSS classes."""
    tree = HTMLParser(html or "")
    out: dict[str, str] = {}
    for a in tree.css("a[href]"):
        href = a.attributes.get("href", "") or ""
        m = id_regex.search(href)
        if m:
            out.setdefault(m.group(1), urljoin(base_url, href.split("?")[0]))
    return list(out.items())


_CAR_JSONLD_TYPES = {"Car", "Vehicle", "Product", "Offer", "IndividualProduct"}


def _jsonld_price(node: dict) -> Any:
    offers = node.get("offers")
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict):
        return offers.get("price", offers.get("priceSpecification", {}).get("price"))
    return node.get("price")


def car_listing_fields(html: str) -> dict:
    """Best-effort car fields from a detail page: JSON-LD -> OpenGraph meta -> text regex.
    Returns keys: title, description, price, currency, image_urls, year, mileage_km, location."""
    title = description = location = None
    price = None
    currency = "CHF"
    images: list[str] = []
    year = mileage = None

    for node in find_json_ld(html):
        types = node.get("@type", "")
        types = types if isinstance(types, list) else [types]
        if not (_CAR_JSONLD_TYPES & set(types)):
            continue
        title = title or node.get("name")
        description = description or node.get("description")
        p = _jsonld_price(node)
        if p is not None and price is None:
            price, currency = parse_price(p)
        img = node.get("image")
        if isinstance(img, str):
            images.append(img)
        elif isinstance(img, list):
            images.extend([i for i in img if isinstance(i, str)])
        odo = node.get("mileageFromOdometer")
        if isinstance(odo, dict):
            mileage = mileage or (int(odo["value"]) if str(odo.get("value", "")).isdigit() else None)
        for k in ("vehicleModelDate", "modelDate", "productionDate", "releaseDate"):
            if node.get(k) and year is None:
                year = parse_year(str(node[k]))

    # OpenGraph / meta fallbacks
    title = title or meta_content(html, "og:title", "twitter:title")
    description = description or meta_content(html, "og:description", "description", "twitter:description")
    if price is None:
        mp = meta_content(html, "product:price:amount", "og:price:amount")
        if mp:
            price, _ = parse_price(mp)
    mc = meta_content(html, "product:price:currency", "og:price:currency")
    if mc:
        currency = mc
    images = images or og_images(html)

    text_blob = f"{title or ''}\n{description or ''}"
    year = year or parse_year(text_blob)
    mileage = mileage or parse_int_km(text_blob)

    return {
        "title": (title or "").strip(),
        "description": (description or "").strip(),
        "price": price,
        "currency": currency,
        "image_urls": images,
        "year": year,
        "mileage_km": mileage,
        "location": location,
    }
