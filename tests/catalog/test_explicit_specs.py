from app.catalog.index.catalog_index import _infer_mechanism
from app.catalog.specs.catalog_specs import extract_water_resistance_m


def test_explicit_mechanism_wins_over_ambiguous_title():
    assert _infer_mechanism("Modelo Automatic", {"mechanism": "Quartz"}) == "quartz"


def test_explicit_water_resistance_with_units_is_preserved():
    assert extract_water_resistance_m({"water_resistance": "10 ATM"}) == 100
    assert extract_water_resistance_m({"name": "Seastar"}) is None
