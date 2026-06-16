"""Data Dragon item data + "completed item" classification.

The lolesports ``details`` feed reports each player's full inventory (item ids) at
every frame — components, boots, trinkets and finished items all mixed together.
To compute *item-completion timing* we need to know which ids are **completed
legendary items** (the big ones that matter), so we can timestamp when the 1st /
2nd / 3rd of them first appears in a player's inventory.

Heuristic for "completed": purchasable, not a Consumable/Trinket, costs at least
``_MIN_GOLD``, and is terminal in the build tree (nothing builds *out* of it — IE
has ``into == []``, whereas B.F. Sword / tier-2 boots build further). Boots are
intentionally excluded (they're not a "big item" for timing purposes). Edge cases
go in ``_INCLUDE`` / ``_EXCLUDE`` rather than complicating the predicate.
"""
import json
import logging
import urllib.request

from backend.config import DDRAGON_FALLBACK_VERSION

log = logging.getLogger(__name__)

_ITEM_JSON_TPL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"

# Minimum total gold for an item to count as a completed legendary.
_MIN_GOLD = 2000

# Manual overrides by item id (tune after inspecting real builds).
_INCLUDE: set[int] = set()   # force-counted as completed
_EXCLUDE: set[int] = set()   # never counted (e.g. an oddball that slips through)

# Cache of {ddragon_version: set(completed_item_ids)} so we fetch each patch once.
_cache: dict[str, set[int]] = {}


def patch_to_ddragon_version(patch: str | None) -> str:
    """Map a livestats patch (``"14.13.601.2810"``) to a DDragon version (``"14.13.1"``).

    Falls back to the configured constant when the patch is missing/odd.
    """
    if not patch:
        return DDRAGON_FALLBACK_VERSION
    parts = patch.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}.{parts[1]}.1"
    return DDRAGON_FALLBACK_VERSION


def completed_item_ids(item_data: dict, min_gold: int = _MIN_GOLD) -> set[int]:
    """Pure classifier: ids of completed legendary items in a DDragon item map.

    ``item_data`` is the ``{"data": {id: {...}}}`` payload of ``item.json``.
    """
    out: set[int] = set()
    for sid, it in item_data.get("data", {}).items():
        try:
            iid = int(sid)
        except (TypeError, ValueError):
            continue
        if iid in _EXCLUDE:
            continue
        if iid in _INCLUDE:
            out.add(iid)
            continue
        gold = it.get("gold", {})
        tags = it.get("tags", []) or []
        if not gold.get("purchasable"):
            continue
        if "Consumable" in tags or "Trinket" in tags:
            continue
        if (gold.get("total") or 0) < min_gold:
            continue
        if it.get("into"):                 # builds into something ⇒ a component
            continue
        out.add(iid)
    return out


def load_completed_ids(patch: str | None) -> set[int]:
    """Completed-item ids for a game's patch (cached; best-effort network fetch).

    Returns an empty set if Data Dragon can't be reached for any candidate version
    — callers then simply record no item timings for that game.
    """
    version = patch_to_ddragon_version(patch)
    for candidate in (version, DDRAGON_FALLBACK_VERSION):
        if candidate in _cache:
            return _cache[candidate]
        try:
            url = _ITEM_JSON_TPL.format(version=candidate)
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            ids = completed_item_ids(data)
            _cache[candidate] = ids
            log.info("item data %s: %d completed items", candidate, len(ids))
            return ids
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("could not load item.json %s (%s)", candidate, exc)
    return set()
