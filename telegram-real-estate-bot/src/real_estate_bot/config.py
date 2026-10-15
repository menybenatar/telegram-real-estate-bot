"""Configuration loading and validation (config.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_BROKER_TERMS = (
    "תיווך",
    "מתווך",
    "מתווכת",
    "משרד תיווך",
    'סוכנות נדל"ן',
    "עמלת תיווך",
)
# Explicit "no broker / from owner" statements. They cancel out broker terms and count as a private-seller claim.
DEFAULT_PRIVATE_CLAIMS = (
    "ללא תיווך",
    "בלי תיווך",
    "מבלי תיווך",
    "ללא מתווך",
    "בלי מתווך",
    "מבלי מתווך",
    "פטור מתיווך",
    "אין תיווך",
    "לא מתווך",
    "אין צורך במתווך",
    "אין צורך בתיווך",
    "ישירות מהבעלים",
    "מבעלים",
)
# Phrases that mention brokerage but say nothing reliable about who the seller is.
DEFAULT_NEUTRAL_NEGATIONS = (
    "ללא עמלת תיווך",
    "ללא דמי תיווך",
    "בלי עמלת תיווך",
    "בלי דמי תיווך",
    "פטור מעמלת תיווך",
)

ALLOWED_TRANSACTIONS = {"sale"}
ALLOWED_PROPERTY_TYPES = {"apartment", "house", "penthouse", "garden_apartment", "duplex"}
ALLOWED_UNCERTAIN = {"include", "exclude", "mark"}
ALLOWED_LANGUAGES = {"en", "he"}


class ConfigError(Exception):
    """Raised with every problem found, so the user can fix them in one go."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors))


@dataclass(frozen=True)
class Settings:
    language: str = "en"
    max_notifications_per_run: int = 10
    send_delay_seconds: float = 1.5
    retention_days: int = 180
    pending_expiry_days: int = 3
    max_send_attempts: int = 3
    request_delay_seconds: float = 3.0
    request_timeout_seconds: float = 20.0
    max_http_retries: int = 3
    respect_robots_txt: bool = True
    notify_on_error: bool = True
    user_agent: str = "real-estate-alert-bot/0.1 (personal, low-volume)"
    broker_terms: tuple[str, ...] = DEFAULT_BROKER_TERMS
    private_claim_phrases: tuple[str, ...] = DEFAULT_PRIVATE_CLAIMS
    neutral_negations: tuple[str, ...] = DEFAULT_NEUTRAL_NEGATIONS


@dataclass(frozen=True)
class SearchConfig:
    name: str
    city: str
    transaction_type: str = "sale"
    property_types: tuple[str, ...] = ("apartment",)
    city_code: int | None = None
    enabled: bool = True
    max_price_ils: int | None = None
    min_price_ils: int | None = None
    min_rooms: float | None = None
    max_rooms: float | None = None
    min_size_sqm: float | None = None
    max_size_sqm: float | None = None
    require_image: bool = True
    require_price: bool = True
    exclude_brokers: bool = True
    broker_uncertain: str = "mark"
    max_pages: int = 3
    extra_params: dict[str, Any] = field(default_factory=dict)

    @property
    def property_type(self) -> str:
        return self.property_types[0]


@dataclass(frozen=True)
class AppConfig:
    settings: Settings
    searches: tuple[SearchConfig, ...]

    @property
    def enabled_searches(self) -> list[SearchConfig]:
        return [s for s in self.searches if s.enabled]


_SETTINGS_KEYS = {f for f in Settings.__dataclass_fields__}
_SEARCH_KEYS = {f for f in SearchConfig.__dataclass_fields__} - {"property_types"} | {"property_type"}


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"cannot read {path}: {exc.strerror or exc}"]) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([f"{path} is not valid YAML: {exc}"]) from exc
    return parse_config(data)


def parse_config(data: Any) -> AppConfig:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ConfigError(["top level of the config must be a mapping with 'searches' (and optional 'settings')"])

    for key in data:
        if key not in {"settings", "searches"}:
            errors.append(f"unknown top-level key '{key}' (allowed: settings, searches)")

    settings = _parse_settings(data.get("settings") or {}, errors)

    searches: list[SearchConfig] = []
    raw_searches = data.get("searches")
    if not isinstance(raw_searches, list) or not raw_searches:
        errors.append("'searches' must be a non-empty list")
    else:
        names: set[str] = set()
        for index, raw in enumerate(raw_searches):
            search = _parse_search(raw, index, errors)
            if search is None:
                continue
            if search.name in names:
                errors.append(f"searches[{index}]: duplicate search name '{search.name}'")
            names.add(search.name)
            searches.append(search)
        if searches and not errors and not any(s.enabled for s in searches):
            errors.append("all searches are disabled; enable at least one")

    if errors:
        raise ConfigError(errors)
    return AppConfig(settings=settings, searches=tuple(searches))


def _parse_settings(raw: Any, errors: list[str]) -> Settings:
    if not isinstance(raw, dict):
        errors.append("'settings' must be a mapping")
        return Settings()
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _SETTINGS_KEYS:
            errors.append(f"settings: unknown key '{key}'")
            continue
        values[key] = value

    where = "settings"
    out: dict[str, Any] = {}
    if "language" in values:
        if values["language"] not in ALLOWED_LANGUAGES:
            errors.append(f"{where}.language must be one of {sorted(ALLOWED_LANGUAGES)}")
        else:
            out["language"] = values["language"]
    for key in ("max_notifications_per_run", "retention_days", "pending_expiry_days", "max_send_attempts", "max_http_retries"):
        if key in values:
            n = _number(values[key], f"{where}.{key}", errors, integer=True, minimum=1)
            if n is not None:
                out[key] = n
    for key in ("send_delay_seconds", "request_delay_seconds", "request_timeout_seconds"):
        if key in values:
            n = _number(values[key], f"{where}.{key}", errors, minimum=0)
            if n is not None:
                out[key] = float(n)
    for key in ("respect_robots_txt", "notify_on_error"):
        if key in values:
            if isinstance(values[key], bool):
                out[key] = values[key]
            else:
                errors.append(f"{where}.{key} must be true or false")
    if "user_agent" in values:
        if isinstance(values["user_agent"], str) and values["user_agent"].strip():
            out["user_agent"] = values["user_agent"].strip()
        else:
            errors.append(f"{where}.user_agent must be a non-empty string")
    for key in ("broker_terms", "private_claim_phrases", "neutral_negations"):
        if key in values:
            v = values[key]
            if isinstance(v, list) and all(isinstance(i, str) and i.strip() for i in v):
                out[key] = tuple(v)
            else:
                errors.append(f"{where}.{key} must be a list of non-empty strings")
    return Settings(**out)


def _parse_search(raw: Any, index: int, errors: list[str]) -> SearchConfig | None:
    label = f"searches[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{label} must be a mapping")
        return None
    if isinstance(raw.get("name"), str) and raw["name"].strip():
        label = f"searches[{index}] ('{raw['name']}')"

    start = len(errors)
    for key in raw:
        if key not in _SEARCH_KEYS:
            errors.append(f"{label}: unknown key '{key}'")

    out: dict[str, Any] = {}
    for required in ("name", "city"):
        value = raw.get(required)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}: '{required}' is required and must be a non-empty string")
        else:
            out[required] = value.strip()

    transaction = raw.get("transaction_type", "sale")
    if transaction not in ALLOWED_TRANSACTIONS:
        errors.append(f"{label}: transaction_type must be one of {sorted(ALLOWED_TRANSACTIONS)} (got {transaction!r})")
    out["transaction_type"] = transaction

    prop = raw.get("property_type", "apartment")
    prop_list = prop if isinstance(prop, list) else [prop]
    if not prop_list or any(p not in ALLOWED_PROPERTY_TYPES for p in prop_list):
        errors.append(f"{label}: property_type must be one or a list of {sorted(ALLOWED_PROPERTY_TYPES)} (got {prop!r})")
    else:
        out["property_types"] = tuple(prop_list)

    if raw.get("city_code") is not None:
        code = _number(raw["city_code"], f"{label}: city_code", errors, integer=True, minimum=1)
        if code is not None:
            out["city_code"] = code

    for key in ("max_price_ils", "min_price_ils"):
        if raw.get(key) is not None:
            n = _number(raw[key], f"{label}: {key}", errors, integer=True, minimum=1)
            if n is not None:
                out[key] = n
    for key in ("min_rooms", "max_rooms", "min_size_sqm", "max_size_sqm"):
        if raw.get(key) is not None:
            n = _number(raw[key], f"{label}: {key}", errors, minimum=0)
            if n is not None:
                out[key] = float(n)
    for lo, hi in (("min_price_ils", "max_price_ils"), ("min_rooms", "max_rooms"), ("min_size_sqm", "max_size_sqm")):
        if lo in out and hi in out and out[lo] > out[hi]:
            errors.append(f"{label}: {lo} ({out[lo]}) is greater than {hi} ({out[hi]})")

    for key in ("enabled", "require_image", "require_price", "exclude_brokers"):
        if key in raw:
            if isinstance(raw[key], bool):
                out[key] = raw[key]
            else:
                errors.append(f"{label}: {key} must be true or false")

    if "broker_uncertain" in raw:
        if raw["broker_uncertain"] in ALLOWED_UNCERTAIN:
            out["broker_uncertain"] = raw["broker_uncertain"]
        else:
            errors.append(f"{label}: broker_uncertain must be one of {sorted(ALLOWED_UNCERTAIN)}")

    if "max_pages" in raw:
        n = _number(raw["max_pages"], f"{label}: max_pages", errors, integer=True, minimum=1)
        if n is not None:
            if n > 20:
                errors.append(f"{label}: max_pages above 20 is not allowed (be polite to the source)")
            else:
                out["max_pages"] = n

    if "extra_params" in raw and raw["extra_params"] is not None:
        if isinstance(raw["extra_params"], dict):
            out["extra_params"] = {str(k): v for k, v in raw["extra_params"].items()}
        else:
            errors.append(f"{label}: extra_params must be a mapping")

    if len(errors) > start:
        return None
    return SearchConfig(**out)


def _number(value: Any, label: str, errors: list[str], *, integer: bool = False, minimum: float = 0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label} must be a number (got {value!r})")
        return None
    if integer and int(value) != value:
        errors.append(f"{label} must be a whole number (got {value!r})")
        return None
    if value < minimum:
        errors.append(f"{label} must be at least {minimum} (got {value!r})")
        return None
    return int(value) if integer else value
