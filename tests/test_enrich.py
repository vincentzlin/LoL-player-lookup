"""Unit tests for the lolesports enrichment logic (pure functions, no network).

Covers the pieces most likely to break: the completed-item classifier, the fingerprint
game resolver, item-timing/level extraction, and the gold-curve → meaningful-lead maths
(including the /window feed's clamp-past-game-end behaviour).
"""
import datetime as dt
from collections import Counter

from backend import items, lolesports
from backend.enrich_lolesports import (
    resolve_game, extract_game, oracle_fingerprints, index_by_date,
    all_have_three_items, lane_opponents, extract_gold_series, meaningful_lead_pct,
    game_lead_pcts, sample_game_gold, _apply_gold,
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


# ── Gold curves / Meaningful lead % ──────────────────────────────────────────

def _gold_meta(pairs, start=START):
    """Minimal index metadata: `pairs` is [(position, blue_champ, red_champ), ...].

    participant_ids follow the feed's convention — 1-5 Blue, 6-10 Red.
    """
    players = {"Blue": [], "Red": []}
    for i, (pos, blue, red) in enumerate(pairs):
        players["Blue"].append({"participant_id": i + 1, "role": pos,
                                "position": pos, "champion": blue})
        players["Red"].append({"participant_id": i + 6, "role": pos,
                               "position": pos, "champion": red})
    return {"esports_game_id": "ES1", "patch": "14.1", "start": start,
            "sides": {s: [p["champion"] for p in v] for s, v in players.items()},
            "players": players}


def test_lane_opponents_pairs_by_position():
    opps = lane_opponents(_gold_meta([("top", "Darius", "Teemo"), ("mid", "Viktor", "Lux")]))
    # Pairing is symmetric: each champion maps to its same-position rival.
    assert opps == {"Darius": "Teemo", "Teemo": "Darius",
                    "Viktor": "Lux", "Lux": "Viktor"}


def test_lane_opponents_skips_duplicated_position():
    """Two champions share a position → no 1-to-1 rival, so the pair is dropped."""
    meta = _gold_meta([("mid", "Viktor", "Lux"), ("mid", "Ziggs", "Syndra")])
    assert lane_opponents(meta) == {}


def test_meaningful_lead_pct_half_the_window():
    # 11 samples (minutes 0-10); the window is minutes 5-10 (6 samples).
    # Ahead at 5,6,7 (ratio 1.2) and level at 8,9,10 → 3/6 = 50%.
    mine = [500] * 5 + [1200, 1200, 1200, 1000, 1000, 1000]
    theirs = [500] * 5 + [1000, 1000, 1000, 1000, 1000, 1000]
    assert meaningful_lead_pct(mine, theirs) == 50.0


def test_meaningful_lead_pct_ignores_the_first_five_minutes():
    """Ahead only before 5:00 → 0%, and the denominator is the window, not the game.

    Guards both halves of the rule: the floor is applied, *and* the early samples are
    excluded from the denominator rather than counted as "not ahead".
    """
    mine = [5000] * 5 + [1000] * 6      # huge lead minutes 0-4, level 5-10
    theirs = [500] * 5 + [1000] * 6
    assert meaningful_lead_pct(mine, theirs) == 0.0


def test_meaningful_lead_pct_ratio_boundary_is_inclusive():
    """The 5% bar is `>=`: 1050 vs 1000 counts, 1049 does not."""
    base = [1000] * 11
    assert meaningful_lead_pct([1000] * 5 + [1050] * 6, base) == 100.0
    assert meaningful_lead_pct([1000] * 5 + [1049] * 6, base) == 0.0


def test_meaningful_lead_pct_short_game_is_none_not_zero():
    """A game ending before the 5:00 floor has no window → None, not 0.0.

    "No data" and "never ahead" are different facts; collapsing them would drag every
    champion's average toward 0.
    """
    assert meaningful_lead_pct([500, 800, 1100], [500, 700, 900]) is None
    assert meaningful_lead_pct([], []) is None


def test_meaningful_lead_pct_zero_opponent_gold():
    """0 opposing gold must not raise: any positive gold is a lead, 0-vs-0 is not."""
    assert meaningful_lead_pct([1] * 11, [0] * 11) == 100.0
    assert meaningful_lead_pct([0] * 11, [0] * 11) == 0.0


def test_extract_gold_series_drops_ragged():
    """A champion missing from a snapshot would misalign index→minute, so it's dropped."""
    meta = _gold_meta([("top", "Darius", "Teemo")])
    snaps = [
        {"ts": _ts(0), "state": "in_game", "gold": {1: 500, 6: 500}},
        {"ts": _ts(60), "state": "in_game", "gold": {1: 900}},          # Teemo (pid 6) missing
        {"ts": _ts(120), "state": "in_game", "gold": {1: 1400, 6: 1200}},
    ]
    series = extract_gold_series(meta, snaps)
    assert series == {"Darius": [500, 900, 1400]}       # Teemo's 2-sample series dropped


def test_extract_gold_series_orders_by_timestamp():
    meta = _gold_meta([("top", "Darius", "Teemo")])
    snaps = [
        {"ts": _ts(120), "state": "in_game", "gold": {1: 1400, 6: 1200}},
        {"ts": _ts(0), "state": "in_game", "gold": {1: 500, 6: 500}},
        {"ts": _ts(60), "state": "in_game", "gold": {1: 900, 6: 800}},
    ]
    assert extract_gold_series(meta, snaps)["Darius"] == [500, 900, 1400]


def test_sample_game_gold_stops_on_finished(monkeypatch):
    """The feed clamps to the final frame forever past game end (HTTP 200, state
    "finished") — unlike /details, which returns None. Without a state check this loops to
    MAX_GOLD_MINUTES and pads the series with frozen end-of-game gold.
    """
    calls = []

    def fake(game_id, when):
        calls.append(when)
        state = "in_game" if len(calls) <= 10 else "finished"
        return {"ts": _ts(60 * len(calls)), "state": state, "gold": {1: 500, 6: 500}}

    monkeypatch.setattr(lolesports, "window_frames_at", fake)
    out = sample_game_gold("ES1", START)
    # 10 in_game samples kept; the finished frame is dropped (its ts is off the grid).
    assert len(out) == 10
    assert all(s["state"] == "in_game" for s in out)
    assert len(calls) == 11                       # stopped, not run to the 60-min hard stop


def test_sample_game_gold_stops_on_missing_frame(monkeypatch):
    monkeypatch.setattr(lolesports, "window_frames_at", lambda g, w: None)
    assert sample_game_gold("ES1", START) == []


def test_game_lead_pcts_skips_champion_without_opponent():
    """A champion whose lane rival has no series can't be scored, so it's omitted."""
    meta = _gold_meta([("top", "Darius", "Teemo"), ("mid", "Viktor", "Lux")])
    series = {"Darius": [1200] * 11, "Teemo": [1000] * 11, "Viktor": [1000] * 11}
    pcts = game_lead_pcts(meta, series)
    assert pcts == {"Darius": 100.0, "Teemo": 0.0}     # Viktor dropped: Lux has no series


def test_apply_gold_writes_only_the_lead_column():
    """The gold pass must never touch item/level — its cache has no item data."""
    row = _row("g", "Darius", "top", "Blue")
    row.level, row.item1_completed_s = 16, 600
    meta = _gold_meta([("top", "Darius", "Teemo")])
    n = _apply_gold({"darius": row}, meta, {"Darius": [1200] * 11, "Teemo": [1000] * 11})
    assert n == 1
    assert row.meaningful_lead_pct == 100.0
    assert row.level == 16 and row.item1_completed_s == 600   # untouched


def test_apply_gold_is_case_insensitive():
    """Feed sends "FiddleSticks"; by_champ is keyed "fiddlesticks" (mirrors _apply)."""
    row = _row("g", "Fiddlesticks", "mid", "Blue")
    meta = _gold_meta([("mid", "FiddleSticks", "Lux")])
    n = _apply_gold({"fiddlesticks": row}, meta,
                    {"FiddleSticks": [1200] * 11, "Lux": [1000] * 11})
    assert n == 1 and row.meaningful_lead_pct == 100.0
