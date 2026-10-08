"""The user-writable settings layer: ``<data home>/settings.json`` (1.3-M2).

Precedence, highest first::

    settings.json  >  environment variables / .env  >  built-in defaults

What it holds (everything optional; a missing entry falls through to the
layer below)::

    {
      "version": 1,
      "providers": {"deepseek": {"api_key": "sk-…", "enabled": true}, …},
      "tiers":     {"cheap": "deepseek-flash", "standard": …, "strong": …},
      "defaults":  {"chat": "cheap", "dispatch": "cheap", "image": "gpt-image-2",
                    "speech": …, "music": …, "transcript": …, "video": …},
      "video":     {"mcp_max_usd": 1.0, "mcp_unlimited": false,
                    "max_wait_minutes": 20, "keep_collecting": true},
      "music":     {"suno_all_tracks": true}
    }

Provider names are the settings slots (``openai``, ``anthropic``, ``gemini``,
``deepseek``, ``openrouter``, ``elevenlabs``, ``kie``). An ``api_key`` of ``""``
means "no key, even if the environment has one".

Keys never leave this module in full: :func:`describe` shows whether a key
is set, its last four characters and where it came from, and error messages
are built from field names, never from values.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from .. import modalities as _MOD
from ..catalog.paths import data_home
from .settings import DefaultModelsSettings, Settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: settings slot -> catalog provider name (the GUI and /api/models use the latter)
PROVIDERS: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google",
    "deepseek": "deepseek",
    "openrouter": "openrouter",
    "elevenlabs": "elevenlabs",
    "kie": "kie",
}
#: catalog provider -> settings slot ("google" -> "gemini")
SLOT_OF: dict[str, str] = {v: k for k, v in PROVIDERS.items()}

TIERS = ("cheap", "standard", "strong")
DEFAULT_KINDS = ("chat", "dispatch") + _MOD.KINDS
#: what each kind falls back to when nothing is configured
BUILTIN_DEFAULTS: dict[str, Optional[str]] = {
    "chat": "cheap",
    "dispatch": "cheap",
    "image": None,  # images.default_model (gpt-image-2)
    "speech": None,  # the first configured speech provider's own default
    "music": None,  # likewise for music
    "transcript": "gpt-transcribe",
    "video": "alibaba/wan-3.0",  # used when it can be called (OpenRouter's roster); else the first usable one (Gemini Omni with a Google key counts)
}
_MOD.require_keys(BUILTIN_DEFAULTS, DEFAULT_KINDS, "user_settings.BUILTIN_DEFAULTS")
# every kind but image has a field of its own (the image default stays in images.default_model)
_MOD.require_keys(set(DefaultModelsSettings.model_fields) | {"image"}, DEFAULT_KINDS, "settings.DefaultModelsSettings")

MAX_KEY_LEN = 500
MAX_MODEL_LEN = 200

#: settings.json ``video`` (1.4-M3): field -> (type, lowest, highest); the
#: defaults are ``config.settings.VideoSettings``'s
VIDEO_FIELDS: dict[str, tuple[type, Optional[float], Optional[float]]] = {
    "mcp_max_usd": (float, 0.01, 1000.0),
    "mcp_unlimited": (bool, None, None),
    "max_wait_minutes": (float, 0.05, 1440.0),
    "keep_collecting": (bool, None, None),
}


#: settings.json ``music``: same shape; defaults in ``config.settings.MusicSettings``
MUSIC_FIELDS: dict[str, tuple[type, Optional[float], Optional[float]]] = {
    "suno_all_tracks": (bool, None, None),
}
#: the plain-value sections of settings.json: name -> its fields
VALUE_SECTIONS: dict[str, dict[str, tuple[type, Optional[float], Optional[float]]]] = {
    "video": VIDEO_FIELDS,
    "music": MUSIC_FIELDS,
}


def _clean_value(section: str, field: str, value: Any) -> Any:
    kind, lo, hi = VALUE_SECTIONS[section][field]
    if kind is bool:
        if not isinstance(value, bool):
            raise SettingsError(f"{section}.{field}: expected true or false")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{section}.{field}: expected a number")
    v = float(value)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise SettingsError(f"{section}.{field}: expected a number from {lo:g} to {hi:g}")
    return v


def _clean_video(field: str, value: Any) -> Any:
    return _clean_value("video", field, value)

#: reading while another process renames the file in place can fail for a moment on Windows
READ_RETRIES = 8
READ_RETRY_DELAY = 0.05


class SettingsError(Exception):
    """A request to change the settings that cannot be applied (HTTP 400)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class SettingsFileError(Exception):
    """settings.json exists but cannot be read or parsed."""


# ---------------------------------------------------------------- file
def settings_path() -> Path:
    return data_home() / "settings.json"


def read_overlay(path: Optional[Path] = None) -> dict[str, Any]:
    """The settings.json content (``{}`` when there is none).

    Retries briefly: on Windows a reader that opens the file while a writer
    replaces it can get ``PermissionError`` (or, rarely, a missing file)."""
    p = path or settings_path()
    last: Optional[BaseException] = None
    for attempt in range(READ_RETRIES):
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError as e:
            if attempt == 0 and not p.exists():
                return {}
            last = e
        except PermissionError as e:
            last = e
        except OSError as e:
            last = e
        else:
            if not text.strip():
                return {}
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                last = e
            else:
                if not isinstance(data, dict):
                    raise SettingsFileError(f"{p} does not hold a JSON object")
                return data
        time.sleep(READ_RETRY_DELAY * (attempt + 1))
    if isinstance(last, FileNotFoundError):
        return {}
    raise SettingsFileError(f"cannot read {p}: {last.__class__.__name__ if last else 'unknown error'}")


def write_overlay(data: dict[str, Any], path: Optional[Path] = None) -> Path:
    """Write settings.json atomically: a temp file in the same folder, flushed
    to disk, then renamed over the old one (retried while a reader holds it)."""
    p = path or settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    body = json.dumps({**data, "version": SCHEMA_VERSION}, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        for attempt in range(20):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return p


# ---------------------------------------------------------------- normalise
def _clean_model(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{where}: expected a non-empty model id")
    v = value.strip()
    if len(v) > MAX_MODEL_LEN or any(ch.isspace() for ch in v):
        raise SettingsError(f"{where}: not a model id")
    return v


def _clean_key(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise SettingsError(f"{where}: expected a string")
    v = value.strip()
    if len(v) > MAX_KEY_LEN or any(ch.isspace() for ch in v):
        raise SettingsError(f"{where}: does not look like an API key (whitespace inside or too long)")
    return v


def normalize_overlay(raw: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
    """Keep the known, well-formed parts of a settings.json object.

    ``strict`` raises :class:`SettingsError` on anything unknown (a PATCH);
    otherwise bad parts are dropped with a warning (a hand-edited file must not
    stop the service from starting)."""
    out: dict[str, Any] = {"providers": {}, "tiers": {}, "defaults": {}, **{name: {} for name in VALUE_SECTIONS}}

    def bad(msg: str) -> None:
        if strict:
            raise SettingsError(msg)
        logger.warning("settings.json: ignoring %s", msg)

    for k in raw:
        if k not in ("version", "providers", "tiers", "defaults", *VALUE_SECTIONS):
            bad(f"unknown section '{k}'")
    provs = raw.get("providers") or {}
    if not isinstance(provs, dict):
        bad("providers: expected an object")
        provs = {}
    for name, p in provs.items():
        slot = SLOT_OF.get(name, name)
        if slot not in PROVIDERS:
            bad(f"providers.{name}: unknown provider")
            continue
        if not isinstance(p, dict):
            bad(f"providers.{name}: expected an object")
            continue
        entry: dict[str, Any] = {}
        for field, v in p.items():
            try:
                if field == "api_key":
                    entry["api_key"] = _clean_key(v, f"providers.{name}.api_key")
                elif field == "enabled":
                    if not isinstance(v, bool):
                        raise SettingsError(f"providers.{name}.enabled: expected true or false")
                    entry["enabled"] = v
                else:
                    raise SettingsError(f"providers.{name}.{field}: unknown field")
            except SettingsError as e:
                bad(str(e))
        if entry:
            out["providers"][slot] = entry
    tiers = raw.get("tiers") or {}
    if not isinstance(tiers, dict):
        bad("tiers: expected an object")
        tiers = {}
    for tier, target in tiers.items():
        if tier not in TIERS:
            bad(f"tiers.{tier}: unknown tier (cheap / standard / strong)")
            continue
        try:
            t = _clean_model(target, f"tiers.{tier}")
            if t in TIERS:
                raise SettingsError(f"tiers.{tier}: a tier must name a model, not another tier")
            out["tiers"][tier] = t
        except SettingsError as e:
            bad(str(e))
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        bad("defaults: expected an object")
        defaults = {}
    for kind, model in defaults.items():
        if kind not in DEFAULT_KINDS:
            bad(f"defaults.{kind}: unknown kind ({' / '.join(DEFAULT_KINDS)})")
            continue
        try:
            out["defaults"][kind] = _clean_model(model, f"defaults.{kind}")
        except SettingsError as e:
            bad(str(e))
    for section, fields in VALUE_SECTIONS.items():
        body = raw.get(section) or {}
        if not isinstance(body, dict):
            bad(f"{section}: expected an object")
            body = {}
        for field, value in body.items():
            if field not in fields:
                bad(f"{section}.{field}: unknown field ({' / '.join(fields)})")
                continue
            try:
                out[section][field] = _clean_value(section, field, value)
            except SettingsError as e:
                bad(str(e))
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- build
def _env_settings(env_file: Optional[Path]) -> Settings:
    return Settings(_env_file=str(env_file) if env_file else None)


def overlay_kwargs(overlay: dict[str, Any], env_only: Settings) -> dict[str, Any]:
    """Turn settings.json into ``Settings(**kwargs)`` init values (pydantic-settings
    deep-merges them over the environment, init values winning)."""
    kw: dict[str, Any] = {}
    providers: dict[str, Any] = {}
    for slot, p in (overlay.get("providers") or {}).items():
        key = p.get("api_key")
        enabled = p.get("enabled")
        if key == "":
            providers[slot] = None  # cleared on purpose: no key, whatever the environment says
            continue
        env_cfg = getattr(env_only.providers, slot, None)
        if not key and not (env_cfg and getattr(env_cfg, "api_key", "")):
            continue  # nothing to switch on or off without a key (kept in the file for later)
        d: dict[str, Any] = {}
        if key:
            d["api_key"] = key
        if enabled is not None:
            d["enabled"] = enabled
        elif key:
            d["enabled"] = True  # a key saved from the settings page is meant to be used
        providers[slot] = d
    if providers:
        kw["providers"] = providers
    if overlay.get("tiers"):
        kw["tiers"] = dict(overlay["tiers"])
    defaults = dict(overlay.get("defaults") or {})
    image = defaults.pop("image", None)
    if image:
        kw["images"] = {"default_model": image}
    if defaults:
        kw["defaults"] = defaults
    for section in VALUE_SECTIONS:
        if overlay.get(section):
            kw[section] = dict(overlay[section])
    return kw


def _field_errors(e: ValidationError) -> str:
    # never echo the input: it may be a key
    return "; ".join(f"{'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg', err.get('type'))}" for err in e.errors(include_input=False))


def build_settings(overlay: Optional[dict[str, Any]] = None, *, env_file: Optional[Path] = None) -> tuple[Settings, Settings]:
    """``(effective, environment only)``. Raises :class:`SettingsError` when the
    combination does not validate."""
    env_only = _env_settings(env_file)
    if not overlay:
        return env_only, env_only
    try:
        effective = Settings(_env_file=str(env_file) if env_file else None, **overlay_kwargs(overlay, env_only))
    except ValidationError as e:
        raise SettingsError(f"the settings do not validate: {_field_errors(e)}") from None
    return effective, env_only


def load_settings(env_file: Any = "auto") -> Settings:
    """Startup: the environment / ``.env`` with settings.json laid over it.

    A broken settings.json never stops the service: it is logged and the
    environment alone is used."""
    from ..layout import env_file as find_env

    env = find_env() if env_file == "auto" else env_file
    try:
        overlay = normalize_overlay(read_overlay())
    except SettingsFileError as e:
        logger.error("settings.json ignored: %s", e)
        overlay = {}
    try:
        effective, _ = build_settings(overlay, env_file=env)
        return effective
    except SettingsError as e:
        logger.error("settings.json ignored: %s", e)
        return _env_settings(env)


# ---------------------------------------------------------------- patch
def apply_patch(overlay: dict[str, Any], patch: Any) -> tuple[dict[str, Any], list[str]]:
    """``(new overlay, changed field paths)`` for a PATCH body.

    The body has the file's shape; a ``null`` removes that entry from
    settings.json (the environment / built-in value shows through again)."""
    if not isinstance(patch, dict):
        raise SettingsError("expected a JSON object")
    unknown = set(patch) - {"providers", "tiers", "defaults", *VALUE_SECTIONS}
    if unknown:
        raise SettingsError(f"unknown fields: {sorted(unknown)}")
    new = json.loads(json.dumps(normalize_overlay(overlay)))
    changed: list[str] = []
    for name, p in (patch.get("providers") or {}).items():
        slot = SLOT_OF.get(name, name)
        if slot not in PROVIDERS:
            raise SettingsError(f"providers.{name}: unknown provider ({', '.join(PROVIDERS)})")
        cur = new.setdefault("providers", {}).setdefault(slot, {})
        if p is None:
            if cur:
                changed += [f"providers.{slot}.{f}" for f in cur]
            new["providers"].pop(slot, None)
            continue
        if not isinstance(p, dict):
            raise SettingsError(f"providers.{name}: expected an object")
        for field, v in p.items():
            if field not in ("api_key", "enabled"):
                raise SettingsError(f"providers.{name}.{field}: unknown field (api_key, enabled)")
            if v is None:
                if field in cur:
                    cur.pop(field)
                    changed.append(f"providers.{slot}.{field}")
                continue
            if field == "api_key":
                v = _clean_key(v, f"providers.{name}.api_key")
            elif not isinstance(v, bool):
                raise SettingsError(f"providers.{name}.enabled: expected true or false")
            if cur.get(field) != v:
                cur[field] = v
                changed.append(f"providers.{slot}.{field}")
        if not cur:
            new["providers"].pop(slot, None)
    for section, allowed in (("tiers", TIERS), ("defaults", DEFAULT_KINDS)):
        body = patch.get(section)
        if body is None:
            continue
        if not isinstance(body, dict):
            raise SettingsError(f"{section}: expected an object")
        cur = new.setdefault(section, {})
        for k, v in body.items():
            if k not in allowed:
                raise SettingsError(f"{section}.{k}: unknown ({' / '.join(allowed)})")
            if v is None:
                if k in cur:
                    cur.pop(k)
                    changed.append(f"{section}.{k}")
                continue
            v = _clean_model(v, f"{section}.{k}")
            if section == "tiers" and v in TIERS:
                raise SettingsError(f"tiers.{k}: a tier must name a model, not another tier")
            if cur.get(k) != v:
                cur[k] = v
                changed.append(f"{section}.{k}")
    for section, fields in VALUE_SECTIONS.items():
        body = patch.get(section)
        if body is None:
            continue
        if not isinstance(body, dict):
            raise SettingsError(f"{section}: expected an object")
        cur = new.setdefault(section, {})
        for k, v in body.items():
            if k not in fields:
                raise SettingsError(f"{section}.{k}: unknown ({' / '.join(fields)})")
            if v is None:
                if k in cur:
                    cur.pop(k)
                    changed.append(f"{section}.{k}")
                continue
            v = _clean_value(section, k, v)
            if cur.get(k) != v:
                cur[k] = v
                changed.append(f"{section}.{k}")
    return normalize_overlay(new, strict=True), changed


_KIND_MODALITY = _MOD.CATALOG_OF


def model_warnings(overlay: dict[str, Any]) -> list[str]:
    """Names in the overlay the catalog does not know (or knows as another
    kind). Saved anyway — a model may be discovered later, an OpenRouter id is
    open-ended — but the page should say so."""
    from ..catalog import catalog

    out = []
    for tier, target in (overlay.get("tiers") or {}).items():
        e = catalog.get(target)
        if e is None and "/" not in target:
            out.append(f"tiers.{tier}: '{target}' is not in the model catalog")
        elif e is not None and e.modality != "text":
            out.append(f"tiers.{tier}: '{target}' is a {e.modality} model, tiers are for text")
    for kind, model in (overlay.get("defaults") or {}).items():
        if kind in ("chat", "dispatch"):
            if model in TIERS or model.startswith(("echo", "replay")) or "/" in model:
                continue
            e = catalog.get(model)
            if e is None:
                out.append(f"defaults.{kind}: '{model}' is not in the model catalog")
            elif e.modality != "text":
                out.append(f"defaults.{kind}: '{model}' is a {e.modality} model")
            continue
        # an OpenRouter id can name a chat model and an image model at once: ask for the kind's own
        e = catalog.get(model, modality=_KIND_MODALITY[kind]) or catalog.get(model)
        if e is None:
            out.append(f"defaults.{kind}: '{model}' is not in the model catalog")
        elif e.modality != _KIND_MODALITY[kind]:
            out.append(f"defaults.{kind}: '{model}' is a {e.modality} model, not {_KIND_MODALITY[kind]}")
    return out


# ---------------------------------------------------------------- describe
def mask(key: Optional[str]) -> dict[str, Any]:
    key = key or ""
    return {"set": bool(key), "last4": key[-4:] if len(key) >= 8 else None}


def _env_var_set(slot: str) -> bool:
    """Is the key in the process environment (not only in the .env file)?"""
    name = f"PROVIDERS__{slot}__API_KEY".upper()
    return any(k.upper() == name and v for k, v in os.environ.items())


def shadowed_key(slot: str, env_key: str) -> Optional[dict[str, Any]]:
    """The environment's key that the settings page's value (a key, or ``""``
    = "no key") is hiding: ``{last4, source: "env", where: env_file|environment}``,
    or ``None``. Removing the settings entry brings it back. Never the key."""
    if not env_key:
        return None
    return {**mask(env_key), "source": "env", "where": "environment" if _env_var_set(slot) else "env_file"}


def provider_health(catalog_name: str, key: str, *, catalog: Any = None) -> Optional[dict[str, Any]]:
    """The provider's last connection result for the key in effect
    (``catalog/health.py``); ``None`` without a key."""
    from ..catalog import health

    if catalog is None:
        from ..catalog import catalog
    try:
        return health.view(catalog_name, key, online_models=catalog.online_count(catalog_name))
    except Exception as e:  # pragma: no cover - informational only
        logger.warning("connection status for %s unavailable: %s", catalog_name, e)
        return None


def health_by_provider(settings: Any) -> dict[str, Optional[dict[str, Any]]]:
    """``{catalog provider: health view}`` for every settings slot (``/api/models``)."""
    out: dict[str, Optional[dict[str, Any]]] = {}
    for slot, cat in PROVIDERS.items():
        cfg = getattr(getattr(settings, "providers", None), slot, None)
        out[cat] = provider_health(cat, (getattr(cfg, "api_key", "") or "") if cfg else "")
    return out


def describe(effective: Settings, env_only: Settings, overlay: dict[str, Any]) -> dict[str, Any]:
    """The GET /api/settings view. Never contains a full key."""
    from ..catalog import catalog
    from ..layout import env_file

    o_prov = overlay.get("providers") or {}
    providers: dict[str, Any] = {}
    labels = catalog.providers
    for slot, cat in PROVIDERS.items():
        eff = getattr(effective.providers, slot, None)
        env = getattr(env_only.providers, slot, None)
        mine = o_prov.get(slot) or {}
        key = getattr(eff, "api_key", "") if eff else ""
        if "api_key" in mine:
            source = "settings"
        elif getattr(env, "api_key", "") if env else "":
            source = "env"
        else:
            source = None
        if "enabled" in mine:
            en_source = "settings"
        elif env is not None and "enabled" in env.model_fields_set:
            en_source = "env"
        else:
            en_source = "default"
        env_key = (getattr(env, "api_key", "") or "") if env else ""
        pcat = labels.get(cat) or {}
        providers[slot] = {
            "provider": cat,
            "label": pcat.get("label") or cat,
            "key": {**mask(key), "source": source if (key or source == "settings") else None,
                    "shadowed": shadowed_key(slot, env_key) if source == "settings" else None},
            "enabled": bool(eff and eff.enabled),
            "enabled_source": en_source,
            "configured": slot in effective.providers.enabled_providers,
            "health": provider_health(cat, key, catalog=catalog),
            "get_key": pcat.get("get_key"),
            "suggested_tiers": pcat.get("suggested_tiers"),
        }
    o_tiers = overlay.get("tiers") or {}
    tiers = {}
    for tier, target in catalog.catalog_tiers.items():
        eff_t = (effective.tiers or {}).get(tier)
        tiers[tier] = {
            "model": eff_t or target,
            "source": "settings" if tier in o_tiers else ("env" if eff_t else "catalog"),
            "catalog": target,
        }
    o_def = overlay.get("defaults") or {}
    defaults = {}
    for kind in DEFAULT_KINDS:
        if kind == "image":
            value = effective.images.default_model
            env_set = "default_model" in env_only.images.model_fields_set
        else:
            value = getattr(effective.defaults, kind)
            env_set = kind in env_only.defaults.model_fields_set
        defaults[kind] = {
            "model": value if value is not None else BUILTIN_DEFAULTS.get(kind),
            "source": "settings" if kind in o_def else ("env" if env_set else "default"),
        }
    values: dict[str, dict[str, Any]] = {}
    for section, fields in VALUE_SECTIONS.items():
        o_sec = overlay.get(section) or {}
        env_sec = getattr(env_only, section, None)
        values[section] = {
            field: {
                "value": getattr(getattr(effective, section), field),
                "source": "settings" if field in o_sec else ("env" if env_sec is not None and field in env_sec.model_fields_set else "default"),
            }
            for field in fields
        }
    env = env_file()
    return {
        "path": str(settings_path()),
        "env_file": str(env) if env else None,
        "providers": providers,
        "tiers": tiers,
        "defaults": defaults,
        **values,
    }


# ---------------------------------------------------------------- the query point for defaults
def default_model(settings: Any, kind: str) -> Optional[str]:
    """The configured default model for ``kind`` (chat, dispatch, image,
    speech, music, transcript), or the built-in one. ``None`` = let the tool
    pick (speech / music: the first configured provider's default).

    Accepts ``None`` or a test double without the new fields."""
    if kind == "image":
        images = getattr(settings, "images", None)
        return getattr(images, "default_model", None) or None
    defaults = getattr(settings, "defaults", None)
    value = getattr(defaults, kind, None) if defaults is not None else None
    return value or BUILTIN_DEFAULTS.get(kind)


# ---------------------------------------------------------------- test a key
#: shorter than this cannot be any vendor's key (the shortest known ones are 32+)
MIN_KEY_LEN = 16


def key_problem(key: str) -> Optional[str]:
    """Why ``key`` cannot be an API key, or ``None``.

    Only what is impossible for any vendor: empty, whitespace or control
    characters inside, non-ASCII (cannot travel in an HTTP header), too short
    or too long. The vendor's prefix (``sk-`` …) is NOT checked: several
    vendors do not document one and Gemini changed its key type in 2026
    (docs/research/2026-10-04-取得key的位置與外部工具安裝方式查證.md)."""
    if not key:
        return "the key is empty"
    if any(ch.isspace() for ch in key):
        return "the key has whitespace or a line break inside (copied with extra text?)"
    if not key.isascii() or not key.isprintable():
        return "the key has characters no API key has (non-ASCII or control characters)"
    if len(key) < MIN_KEY_LEN:
        return f"the key is too short to be an API key ({len(key)} characters)"
    if len(key) > MAX_KEY_LEN:
        return "the key is too long to be an API key"
    return None


async def test_key(slot: str, key: str, settings: Settings, *, timeout: float = 10.0) -> dict[str, Any]:
    """Is ``key`` accepted by ``slot``'s vendor? Uses a free, read-only call
    (list models; OpenRouter's key-info endpoint; kie's credit balance). An
    offline daemon only checks the shape and says so (``simulated``).

    A real answer (not a simulated or a shape check) is filed in the
    provider's connection status (``catalog/health.py``) under this key."""
    import httpx

    from ..catalog import health
    from ..catalog.discovery import fetch_anthropic, fetch_elevenlabs, fetch_gemini, fetch_openai_compatible
    from ..devmode import offline

    out: dict[str, Any] = {"provider": slot, "key": mask(key), "checked_at": time.time()}
    if not key:
        return {**out, "ok": False, "reason": "missing_key", "message": "no key to test"}
    problem = key_problem(key)
    if problem:
        return {**out, "ok": False, "reason": "format", "message": problem, **({"simulated": True} if offline() else {})}
    if offline():
        return {**out, "ok": True, "simulated": True, "reason": None,
                "message": "offline sandbox: nothing was sent; only the key's shape was checked"}
    cfg = getattr(settings.providers, slot, None)
    base = getattr(cfg, "base_url", None)
    catalog_name = PROVIDERS.get(slot, slot)

    def done(result: dict[str, Any], **extra: Any) -> dict[str, Any]:
        health.record(catalog_name, key, ok=bool(result.get("ok")), source="test", at=out["checked_at"],
                      listed=result.get("models"), reason=result.get("reason"), status=result.get("status"),
                      message=result.get("message"), extra=extra or None)
        return result

    try:
        if slot in ("openai", "deepseek"):
            default_base = {"openai": "https://api.openai.com/v1", "deepseek": "https://api.deepseek.com"}[slot]
            models = await fetch_openai_compatible(base or default_base, key, timeout=timeout)
            return done({**out, "ok": True, "models": len(models), "message": f"{len(models)} models listed"})
        if slot == "openrouter":
            # its model list is public, so it proves nothing; GET /key needs a valid key
            url = (base or "https://openrouter.ai/api/v1").rstrip("/") + "/key"
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
            return done({**out, "ok": True, "message": "key accepted"})
        if slot == "anthropic":
            models = await fetch_anthropic(key, base_url=base or "https://api.anthropic.com", timeout=timeout)
            return done({**out, "ok": True, "models": len(models), "message": f"{len(models)} models listed"})
        if slot == "gemini":
            models = await fetch_gemini(key, timeout=timeout)
            return done({**out, "ok": True, "models": len(models), "message": f"{len(models)} models listed"})
        if slot == "elevenlabs":
            models = await fetch_elevenlabs(key, base_url=base or "https://api.elevenlabs.io", timeout=timeout)
            return done({**out, "ok": True, "models": len(models), "message": f"{len(models)} models listed"})
        if slot == "kie":
            return await _test_kie(out, key, base, timeout, done)
        return {**out, "ok": None, "reason": "unsupported", "message": f"there is no free check for {slot}; the key is used on the first real call"}
    except Exception as e:
        return done({**out, "ok": False, **health.classify(e, key=key)})


#: kie.ai's read-only balance query (docs.kie.ai/common-api/get-account-credits):
#: ``GET /api/v1/chat/credit`` with a Bearer key answers ``{code, msg, data: <credits left>}``
KIE_CREDIT_PATH = "/api/v1/chat/credit"


async def _test_kie(out: dict[str, Any], key: str, base: Optional[str], timeout: float, done: Any) -> dict[str, Any]:
    """kie answers HTTP 200 with its own ``code`` in the body, so both are read:
    a 401/403 either way is a rejected key; ``code`` 200 is a good one."""
    import httpx

    url = (base or "https://api.kie.ai").rstrip("/") + KIE_CREDIT_PATH
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if code == 200:
        credits = body.get("data")
        credits = credits if isinstance(credits, (int, float)) and not isinstance(credits, bool) else None
        msg = "key accepted" + (f" ({credits:g} credits left)" if credits is not None else "")
        return done({**out, "ok": True, "credits": credits, "message": msg}, credits=credits)
    if code in (401, 403):
        return done({**out, "ok": False, "reason": "rejected", "status": code, "message": "the provider rejected the key"})
    if isinstance(code, int):
        reason = "payment" if code == 402 else "rate_limited" if code == 429 else "provider_error" if code >= 500 else "http_error"
        return done({**out, "ok": False, "reason": reason, "status": code, "message": f"the provider answered code {code}"})
    return done({**out, "ok": False, "reason": "error", "message": "the provider's answer could not be read"})
