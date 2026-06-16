"""Unit tests for the lolesports enrichment logic (pure functions, no network).

Covers the three pieces most likely to break: the completed-item classifier, the
fingerprint game resolver, and item-timing/level extraction from frames.
"""
import datetime as dt
from collections import Counter

from backend import items
from backend.enrich_lolesports import (
    resolve_game, extract_game, oracle_fingerprints, index_by_date,
    all_have_three_items,
)
from backend.database import PlayerGameStat


UTC = dt.timezone.utc
START = dt.datetime(2024, 7, 19, 10, 37, 0, tzinfo=UTC)


def _ts(rel_s: int) -> str:
    return (START + dt.timedelta(seconds=rel_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── items.completed_item_ids ─────────────────────────────────────────────────

def _item_data():
    # Mirrors the real item.json shape: legendary (IE), component (BF Sword),
    # tier-2 boot (builds further), trinket, consumable.
    return {"data": {
        "3031": {"gold": {"total": 3450, "purchasable": True}, "into": [], "tags": ["Damage"]},
        "1038": {"gold": {"total": 1300, "purchasable": True}, "into": ["3031"], "tags": ["Damage"]},
        "3006": {"gold": {"total": 1100, "purchasable": True}, "into": ["3xxx"], "tags": ["Boots"]},
        "3340": {"gold": {"total": 0, "purchasable": True}, "into": [], "tags": ["Trinket"]},
        "2003": {"gold": {"total": 50, "purchasable": True}, "into": [], "tags": ["Consumable"]},
        "3157": {"gold": {"total": 2600, "purchasable": True}, "into": [], "tags": ["Mana"]},
    }}


def test_completed_item_ids_keeps_only_legendaries():
    ids = items.completed_item_ids(_item_data())
    assert ids == {3031, 3157}            # IE + Zhonya's-like; not BF/boots/trinket/potion


def test_patch_to_ddragon_version():
    assert items.patch_to_ddragon_version("14.13.601.2810") == "14.13.1"
    assert items.patch_to_ddragon_version(None) == items.DDRAGON_FALLBACK_VERSION
    assert items.patch_to_ddragon_version("garbage") == items.DDRAGON_FALLBACK_VERSION


# ── resolve_game ─────────────────────────────────────────────────────────────

def _index_record(esid, day, blue, red):
    return {
        "esports_game_id": esid,
        "start": dt.datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC).isoformat(),
        "sides": {"Blue": blue, "Red": red},
        "patch": "14.13.1.1",
        "players": {},
    }


def test_resolve_matches_on_lineup_and_date():
    blue = ["Vayne", "Trundle", "Cassiopeia", "Ziggs", "Shen"]
    red = ["Ambessa", "LeeSin", "Galio", "Jhin", "Camille"]
    idx = [_index_record("ES1", dt.date(2024, 7, 19), blue, red)]
    by_date = index_by_date(idx)

    fp = {"date": dt.date(2024, 7, 19), "champs": Counter(c.lower() for c in blue + red), "by_champ": {}}
    assert resolve_game(fp, by_date) == "ES1"

    # ±1 day still matches (timezone slack)
    fp_offset = {"date": dt.date(2024, 7, 20), "champs": Counter(c.lower() for c in blue + red), "by_champ": {}}
    assert resolve_game(fp_offset, by_date) == "ES1"


def test_resolve_rejects_different_lineup_and_far_date():
    blue = ["Vayne", "Trundle", "Cassiopeia", "Ziggs", "Shen"]
    red = ["Ambessa", "LeeSin", "Galio", "Jhin", "Camille"]
    idx = [_index_record("ES1", dt.date(2024, 7, 19), blue, red)]
    by_date = index_by_date(idx)

    # one champ different
    other = Counter(c.lower() for c in blue + red[:-1] + ["Karma"])
    assert resolve_game({"date": dt.date(2024, 7, 19), "champs": other, "by_champ": {}}, by_date) is None
    # right lineup, wrong week
    assert resolve_game({"date": dt.date(2024, 8, 1), "champs": Counter(c.lower() for c in blue + red),
                         "by_champ": {}}, by_date) is None


def test_resolve_ambiguous_returns_none():
    blue = ["Vayne", "Trundle", "Cassiopeia", "Ziggs", "Shen"]
    red = ["Ambessa", "LeeSin", "Galio", "Jhin", "Camille"]
    idx = [_index_record("ES1", dt.date(2024, 7, 19), blue, red),
           _index_record("ES2", dt.date(2024, 7, 19), blue, red)]
    by_date = index_by_date(idx)
    fp = {"date": dt.date(2024, 7, 19), "champs": Counter(c.lower() for c in blue + red), "by_champ": {}}
    assert resolve_game(fp, by_date) is None


# ── extract_game ─────────────────────────────────────────────────────────────

def _meta():
    return {
        "esports_game_id": "ES1",
        "patch": "14.13.1.1",
        "start": START,
        "sides": {"Blue": ["Vayne"], "Red": ["Jhin"]},
        "players": {
            "Blue": [{"participant_id": 1, "role": "bottom", "position": "bot",
                      "champion": "Vayne", "summoner": "T1 Gumayusi"}],
            "Red": [{"participant_id": 6, "role": "bottom", "position": "bot",
                     "champion": "Jhin", "summoner": "GEN Ruler"}],
        },
    }


def test_extract_item_timing_and_level():
    completed = {3031, 3153, 6672}
    frames = [
        # t=0: only components
        {"ts": _ts(0), "participants": [
            {"participantId": 1, "level": 1, "items": [1038]},
            {"participantId": 6, "level": 1, "items": [1055]},
        ]},
        # t=720 (12:00): Vayne completes 1st item (6672); Jhin none
        {"ts": _ts(720), "participants": [
            {"participantId": 1, "level": 9, "items": [6672, 1038]},
            {"participantId": 6, "level": 9, "items": [1055, 1018]},
        ]},
        # t=1080 (18:00): Vayne 2nd item (3031); level up
        {"ts": _ts(1080), "participants": [
            {"participantId": 1, "level": 13, "items": [6672, 3031]},
            {"participantId": 6, "level": 12, "items": [3031]},   # different copy on Jhin
        ]},
        # t=1500 (25:00): Vayne 3rd item (3153); final levels
        {"ts": _ts(1500), "participants": [
            {"participantId": 1, "level": 16, "items": [6672, 3031, 3153]},
            {"participantId": 6, "level": 15, "items": [3031]},
        ]},
    ]
    out = extract_game(_meta(), frames, completed)

    vayne = out["Vayne"]
    assert vayne["item1_completed_s"] == 720
    assert vayne["item2_completed_s"] == 1080
    assert vayne["item3_completed_s"] == 1500
    assert vayne["level"] == 16

    jhin = out["Jhin"]
    assert jhin["item1_completed_s"] == 1080      # first completed item at 18:00
    assert jhin["item2_completed_s"] is None
    assert jhin["level"] == 15


def test_all_have_three_items_early_stop():
    completed = {3031, 3153, 6672, 3157}
    seen: dict = {}
    # 10 participants, each already holding 3 completed items → should fire.
    full = {"participants": [
        {"participantId": pid, "items": [3031, 3153, 6672, 1038]} for pid in range(1, 11)
    ]}
    assert all_have_three_items(full, completed, seen) is True

    # Only 9 players itemized, or fewer than 3 each → not yet.
    seen2: dict = {}
    partial = {"participants": [
        {"participantId": pid, "items": [3031, 3153]} for pid in range(1, 11)
    ]}
    assert all_have_three_items(partial, completed, seen2) is False


def test_extract_handles_frames_out_of_order():
    completed = {3031}
    frames = [
        {"ts": _ts(600), "participants": [{"participantId": 1, "level": 8, "items": [3031]}]},
        {"ts": _ts(0), "participants": [{"participantId": 1, "level": 1, "items": [1038]}]},
    ]
    out = extract_game(_meta(), frames, completed)
    assert out["Vayne"]["item1_completed_s"] == 600   # earliest appearance, not file order


# ── oracle_fingerprints ──────────────────────────────────────────────────────

def _row(gameid, champ_dd, pos, side, date="2024-07-19"):
    return PlayerGameStat(gameid=gameid, league="LCK", year=2024, split="Summer",
                          playername=f"p_{champ_dd}", position=pos, side=side,
                          champion=champ_dd, champion_ddragon=champ_dd, date=date)


def test_oracle_fingerprints_groups_by_game():
    rows = [
        _row("ESPORTSTMNT_1", "Vayne", "bot", "Blue"),
        _row("ESPORTSTMNT_1", "Jhin", "bot", "Red"),
        _row("ESPORTSTMNT_2", "Gnar", "top", "Blue"),
    ]
    fps = oracle_fingerprints(rows)
    assert set(fps) == {"ESPORTSTMNT_1", "ESPORTSTMNT_2"}
    # champs + by_champ are keyed by lowercased champion id (case-insensitive join).
    assert fps["ESPORTSTMNT_1"]["champs"] == Counter(["vayne", "jhin"])
    assert fps["ESPORTSTMNT_1"]["date"] == dt.date(2024, 7, 19)
    assert fps["ESPORTSTMNT_1"]["by_champ"]["vayne"].position == "bot"


def test_resolve_is_case_insensitive():
    # Feed sends "FiddleSticks"; Data Dragon / DB store "Fiddlesticks".
    blue = ["FiddleSticks", "Trundle", "Cassiopeia", "Ziggs", "Shen"]
    red = ["Ambessa", "LeeSin", "Galio", "Jhin", "Camille"]
    idx = [_index_record("ES1", dt.date(2024, 7, 19), blue, red)]
    by_date = index_by_date(idx)
    # DB-side fingerprint uses the canonical "Fiddlesticks" casing.
    fp = oracle_fingerprints([
        _row("g", c, "mid", "Blue") for c in
        ["Fiddlesticks", "Trundle", "Cassiopeia", "Ziggs", "Shen",
         "Ambessa", "LeeSin", "Galio", "Jhin", "Camille"]
    ])["g"]
    assert resolve_game(fp, by_date) == "ES1"


def test_apply_is_case_insensitive():
    from backend.enrich_lolesports import _apply
    row = _row("g", "Fiddlesticks", "mid", "Blue")
    row_map = {"fiddlesticks": row}
    n = _apply(row_map, {"FiddleSticks": {
        "item1_completed_s": 600, "item2_completed_s": 900,
        "item3_completed_s": None, "level": 16}})
    assert n == 1
    assert row.level == 16 and row.item1_completed_s == 600
