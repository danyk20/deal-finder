"""same_car: a dealer's ads of one car on two portals, never two very similar cars."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from deal_finder.adapters.base import Listing
from deal_finder.same_car import (
    _SAME_PHOTO_BITS,
    CarFacts,
    _fingerprint,
    car_facts,
    compare,
    fingerprint,
    same_photos,
)


def _as24(**attributes):
    """AutoScout24's ad 20708998 (search results), as its adapter maps it."""
    return Listing(
        marketplace="autoscout24", external_id="20708998", url="https://www.autoscout24.ch/de/d/20708998",
        title="TESLA Model S 100 D", price=19900.0, location="6030 Ebikon- Luzern",
        posted_at=datetime(2026, 8, 14, 15, 8),
        attributes={"year": 2017, "registration_month": "November", "mileage_km": 177900, "fuel": "electric",
                    "transmission": "automatic", "body_color": "black", "horsepower": 525, "power_kw": 386,
                    "doors": 5, "seller_name": "Auto-Center Ebikon AG", "seller_type": "professional",
                    **attributes},
    )


def _autouncle(**attributes):
    """AutoUncle's 7076654 -- the same car's carmarket.ch ad -- as its adapter maps it."""
    return Listing(
        marketplace="autouncle", external_id="7076654", url="https://carmarket.ch/de/fahrzeug/tesla-model-s-100-d-4520703",
        title="Tesla Model S 100 D 2017", price=19900.0, location="6030 Ebikon",
        attributes={"year": 2017, "firstRegistration": "2017-11", "mileage_km": 177900, "fuel": "Elektro",
                    "transmission": "Automatikgetriebe", "color": "Schwarz", "horsepower": 525, "power_kw": 386,
                    "doors": 5, "body_type": "Schrägheck", "first_seen_at": "2026-08-14", **attributes},
    )


def test_the_ebikon_tesla_on_autoscout24_and_carmarket_is_one_car():
    a, b = car_facts(_as24()), car_facts(_autouncle())
    assert a == CarFacts(2017, 177900, 19900, postcode="6030", seller="Auto-Center Ebikon AG",
                         registration="2017-11", horsepower=525, power_kw=386, fuel="electric",
                         gearbox="automatic", colour="black", published=date(2026, 8, 14))
    assert compare(a, b) == (True, ["postcode", "first registration", "power", "fuel", "gearbox", "colour",
                                    "publication date"])


@pytest.mark.parametrize("change, differs", [
    ({"color": "Weiss"}, "colour"),
    ({"firstRegistration": "2017-03"}, "first registration"),
    ({"horsepower": 333, "power_kw": 245}, "power"),
    ({"transmission": "Schaltgetriebe"}, "gearbox"),
    ({"dealer_name": "Garage Meier GmbH"}, "seller"),
])
def test_a_very_similar_car_is_not_the_same_car(change, differs):
    """Same year, mileage and price, but one fact both ads state differs."""
    assert compare(car_facts(_as24()), car_facts(_autouncle(**change))) == (False, [differs])


def test_another_postcode_is_another_car():
    b = car_facts(_autouncle())
    other_town = CarFacts(**{**b.__dict__, "postcode": "8554"})
    assert compare(car_facts(_as24()), other_town)[0] is False


@pytest.mark.parametrize("field, value", [("mileage_km", 177901), ("year", 2018), ("price", 19500)])
def test_year_mileage_and_price_must_be_equal(field, value):
    b = car_facts(_autouncle())
    assert compare(car_facts(_as24()), CarFacts(**{**b.__dict__, field: value})) == (False, [])


def test_too_little_in_common_beyond_year_mileage_and_price_keeps_two_cars():
    """E.g. Facebook (a town, a price, year and km from its text) vs a tutti ad."""
    a = CarFacts(2017, 177900, 19900, postcode="6030", fuel="electric")
    b = CarFacts(2017, 177900, 19900, postcode="6030", fuel="electric", colour="black")
    assert compare(a, b) == (False, ["postcode", "fuel"])


def test_a_matching_vin_is_enough_and_a_different_one_rules_it_out():
    a = CarFacts(2017, 177900, 19900, vin="5YJSA7E26HF123456")
    assert compare(a, CarFacts(2017, 177900, 19900, vin="5YJSA7E26HF123456")) == (True, ["VIN"])
    assert compare(a, CarFacts(2017, 177900, 19900, vin="5YJSA7E26HF654321")) == (False, ["VIN"])


def test_publication_dates_far_apart_dont_make_two_cars():
    """A dealer may add a portal weeks later; AutoUncle dates when it first saw the ad."""
    same, facts = compare(car_facts(_as24()), car_facts(_autouncle(first_seen_at="2026-10-01")))
    assert same and "publication date" not in facts


@pytest.mark.parametrize("x, y", [
    ("Auto-Center Ebikon AG", "Auto Center Ebikon"),
    ("Garage Meier GmbH", "garage meier"),
])
def test_seller_names_written_differently_agree(x, y):
    a = CarFacts(2017, 177900, 19900, seller=x, postcode="6030", fuel="electric")
    b = CarFacts(2017, 177900, 19900, seller=y, postcode="6030", fuel="electric")
    assert compare(a, b) == (True, ["postcode", "seller", "fuel"])


@pytest.mark.parametrize("attributes, expected", [
    ({"fuel": "Plug-in-Hybrid"}, "hybrid"),
    ({"fuel": "Elektro/Benzin"}, "hybrid"),
    ({"fuel": "mhev-petrol"}, "hybrid"),
    ({"fuel": "Benzin"}, "petrol"),
    ({"fuel": "Essence"}, "petrol"),
    ({"transmission": "Automat"}, "automatic"),
    ({"transmission": "Manuell"}, "manual"),
    ({"body_color": "Schwarz metallic"}, "black"),
    ({"color": "dunkelblau"}, "blue"),
    ({"color": "Silber"}, "grey"),  # one group with grey: the portals don't agree on it
    ({"color": "schwarz / beige"}, "black"),  # Autolina: outside / inside
    ({"first_registration": "01.11.2017"}, "2017-11"),
    ({"first_registration": "11/2017"}, "2017-11"),
])
def test_values_in_any_language_or_format_are_compared_alike(attributes, expected):
    facts = car_facts(Listing(marketplace="x", external_id="1", url="", title="", price=19900,
                              attributes={"year": 2017, "mileage_km": 177900, **attributes}))
    assert expected in (facts.fuel, facts.gearbox, facts.colour, facts.registration)


@pytest.mark.parametrize("change", [
    {"price": None},
    {"attributes": {"year": 2017}},
    {"attributes": {"mileage_km": 95000}},
    {"attributes": {"year": 2017, "mileage_km": "95'000 km"}},
    {"attributes": {"year": 2024, "mileage_km": 10}},  # new car: a dealer may have several identical ones
])
def test_no_facts_without_year_mileage_and_price(change):
    base = dict(marketplace="x", external_id="1", url="", title="", price=19900,
                attributes={"year": 2017, "mileage_km": 95000})
    assert car_facts(Listing(**{**base, **change})) is None


def test_facts_round_trip_through_their_stored_text():
    facts = car_facts(_as24())
    assert CarFacts.from_json(facts.to_json()) == facts


@pytest.mark.parametrize("text", ["", "{}", "not json", '{"year": 2017}'])
def test_no_facts_from_empty_or_malformed_text(text):
    assert CarFacts.from_json(text) is None


# --- photos -------------------------------------------------------------------------


def _photo(seed: int, size=(1200, 900), fmt="JPEG", quality=90) -> bytes:
    """A made-up photo: random shapes on a gradient (seed = which photo)."""
    import io
    import random

    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    image = Image.linear_gradient("L").resize((1200, 900)).convert("RGB")
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x, y = rng.randrange(1100), rng.randrange(800)
        draw.rectangle((x, y, x + rng.randrange(60, 400), y + rng.randrange(60, 300)),
                       fill=tuple(rng.randrange(256) for _ in range(3)))
    out = io.BytesIO()
    image.resize(size).save(out, fmt, quality=quality)
    return out.getvalue()


def _bits(x: int, y: int) -> int:
    return bin(x ^ y).count("1")


def test_a_photo_keeps_its_fingerprint_through_resizing_and_re_encoding():
    original = fingerprint(_photo(1, size=(4032, 3024)))
    assert _bits(original, fingerprint(_photo(1, size=(1200, 900), fmt="WEBP", quality=70))) <= _SAME_PHOTO_BITS
    assert _bits(original, fingerprint(_photo(2, size=(4032, 3024)))) > _SAME_PHOTO_BITS * 2


def test_no_fingerprint_for_something_that_isnt_an_image():
    assert fingerprint(b"<html>not found</html>") is None


def _ads(a: list[int], b: list[int]) -> tuple[CarFacts, CarFacts, dict]:
    """Two ads with photos a and b (seeds), their fingerprints already cached (no network);
    seed -1 is a photo that couldn't be loaded."""
    cache = {f"https://a/{i}": None if seed < 0 else fingerprint(_photo(seed)) for i, seed in enumerate(a)}
    cache |= {f"https://b/{i}": None if seed < 0 else fingerprint(_photo(seed, fmt="WEBP")) for i, seed in enumerate(b)}
    ad_a = CarFacts(2017, 177900, 19900, photos=tuple(f"https://a/{i}" for i in range(len(a))))
    ad_b = CarFacts(2017, 177900, 19900, photos=tuple(f"https://b/{i}" for i in range(len(b))))
    return ad_a, ad_b, cache


@pytest.mark.parametrize("a, b", [
    ([1, 2, 3, 4], [1, 2, 3, 4, 5, 6]),  # AutoUncle's 4 = the first 4 on the portal
    ([1, 2, 3, 4], [6, 5, 4, 3, 2, 1]),  # in another order
    ([1, 2, -1], [1, 2, 3]),  # one didn't load: still two of two
])
def test_same_photos(a, b):
    assert same_photos(*_ads(a, b)) is True


@pytest.mark.parametrize("a, b", [
    ([1, 2, 3, 4], [5, 6, 7, 8]),  # a very similar car: other photos
    ([1, 9, 10, 11], [1, 5, 6, 7]),  # only the dealer's banner in common
    ([1, 2, 3, 4, 5, 6], [1, 2, 7, 8, 9, 10]),  # two of six: less than half
    ([1], [1, 2, 3]),  # one photo isn't enough
    ([], [1, 2, 3]),
    ([-1, -1, -1], [1, 2, 3]),  # none loaded
])
def test_not_the_same_photos(a, b):
    assert same_photos(*_ads(a, b)) is False


def test_photos_are_downloaded_once_and_a_failed_one_is_skipped(respx_mock):
    import httpx

    good = respx_mock.get("https://img.test/1.jpg").mock(return_value=httpx.Response(200, content=_photo(1)))
    respx_mock.get("https://img.test/2.jpg").mock(return_value=httpx.Response(404))
    cache: dict = {}
    assert _fingerprint("https://img.test/1.jpg", cache) == fingerprint(_photo(1))
    assert _fingerprint("https://img.test/1.jpg", cache) is not None and good.call_count == 1
    assert _fingerprint("https://img.test/2.jpg", cache) is None


def test_a_car_keeps_its_first_photo_links():
    li = _as24()
    li.image_urls = [f"https://img.test/{i}.jpg" for i in range(20)]
    assert car_facts(li).photos == tuple(f"https://img.test/{i}.jpg" for i in range(6))
    assert CarFacts.from_json(car_facts(li).to_json()) == car_facts(li)
