from pathlib import Path

import pytest

from real_estate_bot.config import ConfigError, load_config, parse_config

ROOT = Path(__file__).resolve().parents[1]


def valid(**search):
    base = {"name": "A", "city": "חולון", "max_price_ils": 1_750_000}
    base.update(search)
    return {"searches": [base]}


def errors_of(data) -> list[str]:
    with pytest.raises(ConfigError) as err:
        parse_config(data)
    return err.value.errors


def test_shipped_config_is_valid():
    cfg = load_config(ROOT / "config.yaml")
    assert [s.name for s in cfg.enabled_searches] == ["Holon apartments for sale"]
    assert cfg.enabled_searches[0].max_price_ils == 1_750_000
    assert len(cfg.searches) == 2  # the disabled example is parsed too


def test_defaults_applied():
    s = parse_config(valid()).searches[0]
    assert s.require_image and s.require_price and s.exclude_brokers and s.property_types == ("apartment",)
    assert s.broker_uncertain == "mark" and s.max_pages == 3


def test_property_type_list_accepted():
    s = parse_config(valid(property_type=["apartment", "house"])).searches[0]
    assert s.property_types == ("apartment", "house")


def test_reports_all_problems_at_once():
    errs = errors_of({"searches": [{"city": "", "max_price_ils": -5, "require_image": "yes", "property_type": "castle"}]})
    joined = "\n".join(errs)
    assert len(errs) >= 4 and "name" in joined and "city" in joined and "max_price_ils" in joined and "castle" in joined


@pytest.mark.parametrize("data,fragment", [
    (None, "mapping"), ({}, "non-empty list"), ({"searches": []}, "non-empty list"),
    ({"searches": "x"}, "non-empty list"), ({"foo": 1, "searches": [{"name": "a", "city": "b"}]}, "unknown top-level key"),
])
def test_structural_errors(data, fragment):
    assert fragment in "\n".join(errors_of(data))


def test_unknown_key_is_caught_as_typo():
    assert "unknown key 'max_prce_ils'" in "\n".join(errors_of(valid(max_prce_ils=1)))


def test_rejects_bad_values():
    assert "transaction_type" in "\n".join(errors_of(valid(transaction_type="rent")))
    assert "greater than" in "\n".join(errors_of(valid(min_rooms=5, max_rooms=3)))
    assert "whole number" in "\n".join(errors_of(valid(max_price_ils=1.5)))
    assert "broker_uncertain" in "\n".join(errors_of(valid(broker_uncertain="maybe")))
    assert "max_pages" in "\n".join(errors_of(valid(max_pages=100)))
    assert "number" in "\n".join(errors_of(valid(max_price_ils=True)))


def test_duplicate_names_and_all_disabled():
    two = {"searches": [{"name": "A", "city": "x"}, {"name": "A", "city": "y"}]}
    assert "duplicate" in "\n".join(errors_of(two))
    assert "disabled" in "\n".join(errors_of(valid(enabled=False)))


def test_settings_validation():
    data = valid()
    data["settings"] = {"language": "fr", "retention_days": 0, "nope": 1, "broker_terms": "x"}
    joined = "\n".join(errors_of(data))
    assert "language" in joined and "retention_days" in joined and "unknown key 'nope'" in joined and "broker_terms" in joined


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("searches: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
