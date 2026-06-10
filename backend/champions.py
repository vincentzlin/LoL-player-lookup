"""Champion name → Data Dragon id normalization and image URL helpers.

Oracle's Elixir mostly uses display names that match Data Dragon ids, but a
handful differ (apostrophes, spaces, "Wukong" vs "MonkeyKing", etc.).
"""
import logging
import urllib.request

from backend.config import (
    DDRAGON_VERSIONS_URL, DDRAGON_IMG_TPL, DDRAGON_FALLBACK_VERSION,
)

log = logging.getLogger(__name__)

# Explicit overrides where the Oracle name != Data Dragon id.
_OVERRIDES = {
    "Wukong": "MonkeyKing",
    "Nunu & Willump": "Nunu",
    "Nunu": "Nunu",
    "Renata Glasc": "Renata",
    "Dr. Mundo": "DrMundo",
    "Tahm Kench": "TahmKench",
    "Kog'Maw": "KogMaw",
    "Cho'Gath": "Chogath",
    "Kai'Sa": "Kaisa",
    "Kha'Zix": "Khazix",
    "Vel'Koz": "Velkoz",
    "Rek'Sai": "RekSai",
    "Bel'Veth": "Belveth",
    "K'Sante": "KSante",
    "LeBlanc": "Leblanc",
    "Jarvan IV": "JarvanIV",
    "Lee Sin": "LeeSin",
    "Master Yi": "MasterYi",
    "Miss Fortune": "MissFortune",
    "Twisted Fate": "TwistedFate",
    "Xin Zhao": "XinZhao",
    "Aurelion Sol": "AurelionSol",
    "Tahm  Kench": "TahmKench",
}

_version = DDRAGON_FALLBACK_VERSION


def refresh_version() -> str:
    """Fetch the latest Data Dragon version once (best-effort)."""
    global _version
    try:
        with urllib.request.urlopen(DDRAGON_VERSIONS_URL, timeout=8) as resp:
            import json
            versions = json.loads(resp.read().decode())
            if versions:
                _version = versions[0]
                log.info("Data Dragon version: %s", _version)
    except Exception as exc:  # noqa: BLE001 - best effort, fall back to constant
        log.warning("Could not fetch Data Dragon version (%s); using %s",
                    exc, _version)
    return _version


def to_ddragon_id(champion: str | None) -> str:
    """Normalize an Oracle champion name to a Data Dragon id."""
    if not champion:
        return ""
    name = champion.strip()
    if name in _OVERRIDES:
        return _OVERRIDES[name]
    # Default: strip spaces, apostrophes, dots; keep original casing of words.
    cleaned = (
        name.replace("'", "")
        .replace(".", "")
        .replace("&", "")
        .replace("  ", " ")
    )
    parts = [p for p in cleaned.split(" ") if p]
    if len(parts) == 1:
        return parts[0]
    # Multi-word: capitalize each word, concatenate (e.g. "Xin Zhao" -> "XinZhao")
    return "".join(p[0].upper() + p[1:] for p in parts)


def image_url(champion: str | None) -> str:
    ddragon = to_ddragon_id(champion)
    if not ddragon:
        return ""
    return DDRAGON_IMG_TPL.format(version=_version, champ=ddragon)
