"""
regional_persona.py  --  Regional QA Test Persona (authorized testing only).

Isolated, dependency-free helper for the MightyGrid app-layer QA flow. It loads
a small JSON catalogue of *regional test profiles* (country/region/city/timezone/
locale/lat-long) and returns the one selected for a run, so the existing 4.2
app-layer overrides (timezone, locale, test location) can be driven from a chosen
region instead of hardcoded values.

Deliberately NOT included, by design:
  - No proxy connection, no proxy rotation, no exit-IP verification.
  - No GeoIP lookup and no "make the spoofed signals agree with a detected IP"
    coherence enforcement. The source of truth is the region the tester picks,
    not a detected network identity. This module performs zero network I/O.

The profile's optional `test_endpoint` is treated as a reference string only
(shown in logs with credentials masked); this module never connects to it.
"""

import json
import os

# Fields a profile may carry. Only these are read; unknown keys are ignored.
_FIELDS = (
    "id", "country", "country_code", "region", "city",
    "timezone", "locale", "latitude", "longitude",
    "test_endpoint", "enabled",
)


class RegionalProfileError(Exception):
    """Raised for missing files, malformed JSON, or unknown/disabled profile ids."""


def load_profiles(path):
    """
    Load and lightly validate the regional profile catalogue.

    Accepts either {"profiles": [ ... ]} or a bare list [ ... ].
    Returns a list of profile dicts. Raises RegionalProfileError on any problem.
    """
    if not os.path.exists(path):
        raise RegionalProfileError(f"profiles file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise RegionalProfileError(f"could not parse {os.path.basename(path)}: {e}")

    profiles = data.get("profiles") if isinstance(data, dict) else data
    if not isinstance(profiles, list) or not profiles:
        raise RegionalProfileError("no 'profiles' list found in catalogue")

    cleaned = []
    seen = set()
    for i, raw in enumerate(profiles):
        if not isinstance(raw, dict):
            raise RegionalProfileError(f"profile #{i} is not an object")
        pid = str(raw.get("id", "")).strip()
        if not pid:
            raise RegionalProfileError(f"profile #{i} is missing 'id'")
        if pid in seen:
            raise RegionalProfileError(f"duplicate profile id: {pid}")
        seen.add(pid)
        prof = {k: raw.get(k) for k in _FIELDS if k in raw}
        prof["id"] = pid
        prof["enabled"] = bool(raw.get("enabled", True))
        cleaned.append(prof)
    return cleaned


def select_profile(profiles, region_id=None):
    """
    Select exactly one profile.

      - region_id given: return that profile if present AND enabled.
      - region_id omitted: return the first enabled profile.

    Deliberate selection only -- no random / round-robin rotation (that belongs
    to evasion tooling, not to picking a region to test). Raises on no match.
    """
    if region_id:
        rid = str(region_id).strip()
        for p in profiles:
            if p["id"] == rid:
                if not p.get("enabled", True):
                    raise RegionalProfileError(f"profile '{rid}' is disabled")
                return p
        available = ", ".join(p["id"] for p in profiles) or "(none)"
        raise RegionalProfileError(f"unknown profile id '{rid}'. Available: {available}")

    for p in profiles:
        if p.get("enabled", True):
            return p
    raise RegionalProfileError("no enabled profiles in catalogue")


def _utc_offset(timezone):
    """Best-effort IANA -> current UTC offset string (e.g. '-07:00'). Never raises."""
    if not timezone:
        return None
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        off = datetime.now(ZoneInfo(str(timezone))).utcoffset()
        if off is None:
            return None
        total = int(off.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"
    except Exception:
        return None


def mask_endpoint(url):
    """Return a log-safe copy of a URL/endpoint with any user:pass credentials masked."""
    if not url:
        return url
    s = str(url)
    if "@" in s and "://" in s:
        scheme, rest = s.split("://", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0] if ":" in creds else creds
        return f"{scheme}://{user}:****@{host}"
    return s


def build_persona(profile):
    """
    Normalise a selected profile into the RegionalPersona dict consumed by the
    QA flow. Values come straight from the chosen profile (configuration), not
    from any network lookup. `utc_offset` is derived from the timezone if the
    platform tz database is available, otherwise None.
    """
    persona = {k: profile.get(k) for k in _FIELDS}
    persona["id"] = profile["id"]
    if persona.get("latitude") is not None:
        persona["latitude"] = str(persona["latitude"])
    if persona.get("longitude") is not None:
        persona["longitude"] = str(persona["longitude"])
    persona["utc_offset"] = _utc_offset(persona.get("timezone"))
    return persona