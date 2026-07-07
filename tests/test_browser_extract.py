from __future__ import annotations

from deal_finder.browser import extract as ex


def test_parse_price_variants():
    assert ex.parse_price("CHF 38'900.-") == (38900.0, "CHF")
    assert ex.parse_price("Fr. 27500") == (27500.0, "CHF")
    assert ex.parse_price({"amount": 45000}) == (45000.0, "CHF")
    assert ex.parse_price("€ 45.900")[0] == 45900.0
    assert ex.parse_price("€ 45.900")[1] == "EUR"
    assert ex.parse_price(None) == (None, "CHF")


def test_parse_km_and_year():
    assert ex.parse_int_km("Occasion, 95'000 km, top") == 95000
    assert ex.parse_int_km("120 000 km") == 120000
    assert ex.parse_int_km("no mileage here") is None
    assert ex.parse_year("Erstzulassung 2017, gepflegt") == 2017
    assert ex.parse_year("kein Jahr") is None
