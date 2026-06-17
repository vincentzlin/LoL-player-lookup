"""Tests for the champion draft graph model (VIN-20).

Seeded data (see conftest ``_draft_rows``): three 2024 'Spring' games where team
Alpha (Darius / Sejuani / Orianna / Ashe / Lulu) beats team Bravo (Teemo / Vi /
Syndra / Jinx / Thresh). Every team has only 3 games (< MIN_TEAM_GAMES), so all
ratings are 0, the expected score is 0.5, and each margin is ±0.5. With K_SHRINK=6
a pair seen 3× on the winning side has weight 0.5*3/(3+6)*100 = 16.7%.

The endpoints query season 14 (= 2024) Spring to isolate this data.
"""
import math

from backend.api import draft

TF = {"season": 14, "split": "Spring"}
FALL = {"season": 14, "split": "Fall"}
EXPECTED_W = 16.7     # round(0.5 * 3 / (3 + draft.K_SHRINK) * 100, 1)


# ── Pure skill-adjustment math ────────────────────────────────────────────────

def test_expected_score_symmetry_and_direction():
    assert draft.expected_score(0, 0) == 0.5
    assert draft.expected_score(400, 0) > 0.5          # stronger team favoured
    assert draft.expected_score(0, 400) < 0.5


def test_strong_win_counts_less_than_an_upset():
    """A favourite winning earns a smaller margin than an underdog upset."""
    strong, weak = 400.0, -400.0
    favourite_win_margin = 1.0 - draft.expected_score(strong, weak)
    upset_win_margin = 1.0 - draft.expected_score(weak, strong)
    assert favourite_win_margin < 0.5 < upset_win_margin


def test_rating_from_winrate_needs_min_games():
    assert draft._rating_from_winrate(3, 3) == 0.0     # under MIN_TEAM_GAMES
    assert draft._rating_from_winrate(8, 10) > 0.0     # 80% over enough games
    assert draft._rating_from_winrate(2, 10) < 0.0     # 20%


# ── /api/champions ────────────────────────────────────────────────────────────

def test_champions_endpoint_lists_seeded_champions(client):
    champs = {c["champion"]: c for c in client.get("/api/champions").json()}
    assert {"Darius", "Ashe", "Lulu", "Teemo"} <= set(champs)
    assert champs["Ashe"]["image_url"].endswith("/Ashe.png")


def test_unknown_champion_is_404(client):
    assert client.get("/api/champion/NotAChampion/graph").status_code == 404


def test_champion_lookup_is_case_insensitive(client):
    d = client.get("/api/champion/darius/graph", params=TF).json()
    assert d["champion"] == "Darius"


# ── Synergy edges ─────────────────────────────────────────────────────────────

def test_synergy_is_positive_for_winning_teammates(client):
    d = client.get("/api/champion/Ashe/graph", params=TF).json()
    syn = {e["champion"]: e for e in d["synergies"]}
    assert "Lulu" in syn
    assert syn["Lulu"]["weight"] == EXPECTED_W
    assert syn["Lulu"]["games"] == 3


def test_synergy_excludes_opponents(client):
    """Teemo (enemy) must not appear among Ashe's synergies."""
    d = client.get("/api/champion/Ashe/graph", params=TF).json()
    assert "Teemo" not in {e["champion"] for e in d["synergies"]}


# ── Counter edges ─────────────────────────────────────────────────────────────

def test_counter_is_favourable_for_the_winning_side(client):
    d = client.get("/api/champion/Darius/graph", params=TF).json()
    cnt = {e["champion"]: e for e in d["counters"]}
    assert cnt["Teemo"]["weight"] == EXPECTED_W       # Darius's side beat Teemo's


def test_counter_is_antisymmetric(client):
    darius = {e["champion"]: e["weight"]
              for e in client.get("/api/champion/Darius/graph", params=TF).json()["counters"]}
    teemo = {e["champion"]: e["weight"]
             for e in client.get("/api/champion/Teemo/graph", params=TF).json()["counters"]}
    assert darius["Teemo"] == -teemo["Darius"]


def test_losing_side_has_negative_edges(client):
    d = client.get("/api/champion/Teemo/graph", params=TF).json()
    syn = {e["champion"]: e["weight"] for e in d["synergies"]}
    assert syn["Vi"] == -EXPECTED_W                    # lost together all 3 games


def test_synergies_include_worst_teammates(client):
    """Synergies now span best→worst; a losing champ's edges are all negative."""
    d = client.get("/api/champion/Jinx/graph", params=TF).json()
    assert d["synergies"]                               # not empty
    assert all(e["weight"] < 0 for e in d["synergies"])


# ── Win rates ─────────────────────────────────────────────────────────────────

def test_win_rate_and_adjusted_for_neutral_strength(client):
    """All teams here have 3 games (rating 0, expected 0.5) → adjusted == raw."""
    ashe = client.get("/api/champion/Ashe/graph", params=TF).json()["stats"]
    assert ashe["games"] == 3
    assert ashe["win_rate"] == 100.0 and ashe["adjusted_win_rate"] == 100.0
    jinx = client.get("/api/champion/Jinx/graph", params=TF).json()["stats"]
    assert jinx["win_rate"] == 0.0 and jinx["adjusted_win_rate"] == 0.0


def test_adjusted_win_rate_recenters_for_a_strong_team(client):
    """Riven rides a strong team (80% raw) but only meets expectations → adj 50%."""
    s = client.get("/api/champion/Riven/graph",
                   params={"season": 14, "split": "Summer"}).json()["stats"]
    assert s["win_rate"] == 80.0
    assert s["adjusted_win_rate"] == 50.0
    assert s["adjusted_win_rate"] < s["win_rate"]


# ── Duration / dragon splits + GD@15 + pairing recompute (Caitlyn fixture) ─────

WINTER = {"season": 14, "split": "Winter"}


def test_overall_gd15_and_splits(client):
    s = client.get("/api/champion/Caitlyn/graph", params=WINTER).json()["stats"]
    assert s["games"] == 4 and s["win_rate"] == 50.0
    assert s["gd15"] == 50.0                              # mean(100,300,-200,0)
    dur = {x["min_minutes"]: x for x in s["duration_splits"]}
    assert dur[25]["games"] == 3 and dur[30]["games"] == 2 and dur[35]["games"] == 1
    assert dur[30]["win_rate"] == 0.0                     # both >30min games are losses
    drag = {x["bucket"]: x for x in s["dragon_splits"]}
    assert set(drag) == {"0", "1", "2", "4+"}            # no 3-dragon game
    assert drag["1"]["win_rate"] == 100.0 and drag["4+"]["win_rate"] == 0.0


def test_pairing_recomputes_for_synergy(client):
    d = client.get("/api/champion/Caitlyn/pairing",
                   params={**WINTER, "other": "Lux", "kind": "synergy"}).json()
    assert d["other"]["champion"] == "Lux" and d["kind"] == "synergy"
    assert d["stats"]["games"] == 3                       # Lux co-occurs in g1-3
    assert d["stats"]["gd15"] == 66.7                     # mean(100,300,-200)
    assert d["overall"]["gd15"] == 50.0                   # differs from overall
    assert d["stats"]["win_rate"] == 66.7


def test_throw_index_helper():
    """Mid-rank percentile; None when too few peers or no value."""
    assert draft._throw_index(75, [75, 0, 0]) == 83.3   # (2 below + 0.5 tie)/3
    assert draft._throw_index(0, [75, 0, 0]) == 33.3    # (0 below + 1 tie)/3
    assert draft._throw_index(75, [75]) is None         # < 3 peers
    assert draft._throw_index(None, [1, 2, 3]) is None


def test_throwing_factor_metric(client):
    """One 300g throw over 4 games → 75 gold/game; index 83.3 vs Winter peers."""
    s = client.get("/api/champion/Caitlyn/graph", params=WINTER).json()["stats"]
    assert s["swing_games"] == 4
    assert s["throw_gold_pg"] == 75.0      # 300 thrown / 4 games played
    assert s["throw_count"] == 1           # only c2: ahead at 15 (+300) then 0
    assert s["throw_rate"] == 25.0
    assert s["avg_throw_size"] == 300.0
    # peers (Winter, all roles, ≥3 games): Caitlyn 75, Lux 0, Jinx 0 → mid-rank of 75
    assert s["throwing_factor"] == 83.3


def test_throwing_factor_in_pairing(client):
    """Lux co-occurs in c1-3: 300 thrown / 3 games = 100 gold/game → index 100."""
    d = client.get("/api/champion/Caitlyn/pairing",
                   params={**WINTER, "other": "Lux", "kind": "synergy"}).json()
    assert d["stats"]["swing_games"] == 3
    assert d["stats"]["throw_gold_pg"] == 100.0
    assert d["stats"]["throwing_factor"] == 100.0      # raw 100 > all peers
    assert d["overall"]["throwing_factor"] == 83.3     # overall ranks lower


def test_logistic_breakeven_helper():
    """Symmetric separable data → crossover at the midpoint; degenerate → None."""
    diffs = [0.0] * 12 + [2000.0] * 12
    wins = [False] * 12 + [True] * 12
    off = [0.0] * 24
    be = draft._logistic_breakeven(diffs, wins, off)
    assert be is not None and 850 <= be <= 1150        # midpoint ≈ 1000 (unrounded)
    assert draft._logistic_breakeven(diffs, [True] * 24, off) is None   # one class
    assert draft._logistic_breakeven([0.0] * 10, [True, False] * 5, [0.0] * 10) is None
    # reversed trend (more gold → losses) → no positive slope → None
    assert draft._logistic_breakeven(diffs, [True] * 12 + [False] * 12, off) is None


def test_edges_trims_outliers():
    """Edges = min/max of games within ±3 SD; a freak game is excluded."""
    assert draft._edges([0.0] * 15 + [100.0] * 15) == (0.0, 100.0)
    assert draft._edges([0.0] * 15 + [100.0] * 15 + [10000.0]) == (0.0, 100.0)


def test_edge_clamp():
    """Clamp the raw break-even to the observed edges; flag when it was clamped."""
    diffs = [0.0] * 15 + [100.0] * 15            # edges (0, 100)
    assert draft._edge_clamp(50.0, diffs, 50) == (50, False)    # inside
    assert draft._edge_clamp(300.0, diffs, 50) == (100, True)   # above best game
    assert draft._edge_clamp(-300.0, diffs, 50) == (0, True)    # below worst game
    assert draft._edge_clamp(None, diffs, 50) == (None, False)


def test_logistic_breakeven_adjustment_shifts():
    """A team-favoured offset raises the break-even (champ must lead more to be even)."""
    diffs = [0.0] * 12 + [2000.0] * 12
    wins = [False] * 12 + [True] * 12
    base = draft._logistic_breakeven(diffs, wins, [0.0] * 24)
    fav = math.log(0.8 / 0.2)
    adj = draft._logistic_breakeven(diffs, wins, [fav] * 24)
    assert base is not None and adj is not None and adj > base


def test_when_ahead_stat(client):
    """Viktor: 50% adjusted WR needs ~1000 gold / ~750 xp / ~1000 team-gold lead.

    All break-evens fall within the champion's game range, so none are clamped.
    """
    pts = {p["minute"]: p for p in client.get(
        "/api/champion/Viktor/graph",
        params={"season": 14, "split": "WhenAhead"}).json()["stats"]["when_ahead"]}
    assert set(pts) == {15, 20, 25}
    assert 850 <= pts[15]["break_even_gold"] <= 1150
    assert 600 <= pts[15]["break_even_xp"] <= 900
    assert 850 <= pts[15]["break_even_team_gold"] <= 1150
    assert pts[15]["break_even_gold_capped"] is False
    assert pts[15]["break_even_team_gold_capped"] is False


def test_when_ahead_in_pairing(client):
    """Pairing detail carries When Ahead (incl. team gold) for both subset & overall.

    Karma co-occurs in all 24 Viktor games, so the subset is well-sampled.
    """
    d = client.get("/api/champion/Viktor/pairing",
                   params={"season": 14, "split": "WhenAhead",
                           "other": "Karma", "kind": "synergy"}).json()
    sub = {p["minute"]: p for p in d["stats"]["when_ahead"]}
    assert 850 <= sub[15]["break_even_gold"] <= 1150
    assert 850 <= sub[15]["break_even_team_gold"] <= 1150
    # overall block also carries the When Ahead points
    over = {p["minute"]: p for p in d["overall"]["when_ahead"]}
    assert over[15]["break_even_gold"] is not None


def test_when_ahead_na_below_min_sample(client):
    """Caitlyn has only 4 games → all break-evens are N/A."""
    pts = client.get("/api/champion/Caitlyn/graph", params=WINTER).json()["stats"]["when_ahead"]
    assert pts and all(p["break_even_gold"] is None and p["break_even_xp"] is None
                       and p["break_even_team_gold"] is None for p in pts)


def test_pairing_recomputes_for_counter(client):
    d = client.get("/api/champion/Caitlyn/pairing",
                   params={**WINTER, "other": "Jinx", "kind": "counter"}).json()
    assert d["kind"] == "counter" and d["stats"]["games"] == 3
    assert d["stats"]["gd15"] == 66.7


def test_pairing_bad_inputs(client):
    assert client.get("/api/champion/Caitlyn/pairing",
                      params={"other": "Lux", "kind": "bogus"}).status_code == 400
    assert client.get("/api/champion/Caitlyn/pairing",
                      params={"other": "NotAChamp", "kind": "synergy"}).status_code == 404


# ── Role split (Graves: jng on a winning team, top on a losing one) ────────────

def test_roles_summary_separates_win_rates(client):
    d = client.get("/api/champion/Graves/graph", params=FALL).json()  # all roles
    roles = {r["role"]: r for r in d["roles"]}
    assert set(roles) == {"jng", "top"}
    assert roles["jng"]["win_rate"] == 100.0 and roles["jng"]["games"] == 3
    assert roles["top"]["win_rate"] == 0.0 and roles["top"]["games"] == 3
    assert roles["jng"]["role_label"] == "Jungle"
    # all-roles view merges both
    assert d["role"] is None and d["stats"]["games"] == 6 and d["stats"]["win_rate"] == 50.0


def test_synergies_filtered_by_selected_role(client):
    jng = client.get("/api/champion/Graves/graph",
                     params={**FALL, "role": "jng"}).json()
    names = {e["champion"] for e in jng["synergies"]}
    assert "Karma" in names          # jng teammate, 3 games
    assert "Lux" not in names        # only a teammate in the top role
    assert "Nami" not in names       # jng teammate but only 2 games (< min 3)
    assert jng["role"] == "jng" and jng["stats"]["win_rate"] == 100.0

    top = client.get("/api/champion/Graves/graph",
                     params={**FALL, "role": "top"}).json()
    tsyn = {e["champion"]: e["weight"] for e in top["synergies"]}
    assert "Lux" in tsyn and tsyn["Lux"] < 0
    assert "Karma" not in tsyn
    assert top["stats"]["win_rate"] == 0.0


def test_synergy_requires_min_three_games(client):
    """Nami (2 games with Graves) is excluded even in the merged all-roles view."""
    d = client.get("/api/champion/Graves/graph", params=FALL).json()
    names = {e["champion"] for e in d["synergies"]}
    assert "Karma" in names
    assert "Nami" not in names


def test_counters_filtered_by_role(client):
    jng = client.get("/api/champion/Graves/graph",
                     params={**FALL, "role": "jng"}).json()
    cnt = {e["champion"]: e for e in jng["counters"]}
    assert cnt["Galio"]["weight"] > 0 and cnt["Galio"]["games"] == 3
