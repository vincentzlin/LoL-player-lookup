"""API behaviour tests for the LCK pro-player stats app."""


def test_players_list(client):
    data = client.get("/api/players").json()
    names = {p["name"] for p in data}
    assert names == {"Teddy", "Ruler", "Kiin", "Zeus"}
    zeus = next(p for p in data if p["name"] == "Zeus")
    assert zeus["role"] == "top" and zeus["role_label"] == "Top"


def test_unknown_player_is_404(client):
    assert client.get("/api/player/Faker/stats").status_code == 404
    assert client.get("/api/player/Faker/filters").status_code == 404


def test_legacy_search_path_absent(client):
    """Regression guard: the sibling project's /api/search must NOT exist here.

    A cached sibling frontend calling /api/search is the root cause of the
    reported 404. This documents that the path is intentionally absent.
    """
    assert client.get("/api/search", params={"q": "Teddy"}).status_code == 404


def test_search_flow_endpoints_work(client):
    """The endpoints the real frontend actually uses for 'search'."""
    assert client.get("/api/players").status_code == 200
    assert client.get("/api/player/Teddy/filters").status_code == 200


def test_filters_reflect_available_seasons(client):
    f = client.get("/api/player/Zeus/filters").json()
    seasons = [s["season"] for s in f["seasons"]]
    assert seasons == [15, 14]                       # newest first
    assert f["splits_by_season"]["15"][0]["value"] == "Spring"


def test_ruler_excludes_non_lck_season(client):
    """Ruler only has LCK 2025 data seeded -> only Season 15 offered."""
    seasons = [s["season"] for s in client.get("/api/player/Ruler/filters").json()["seasons"]]
    assert seasons == [15]


def test_overall_metrics(client):
    d = client.get("/api/player/Zeus/stats",
                   params={"season": 15, "split": "Spring"}).json()
    o = d["overall"]
    assert o["games"] == 4
    assert o["kills"] == 2.25            # (4+3+2+0)/4
    assert o["deaths"] == 1.5            # (2+1+3+0)/4
    assert o["assists"] == 7.25          # (6+8+5+10)/4
    assert o["kda"] == 6.333             # aggregate (9+29)/6
    assert o["cspm"] == 8.5
    assert o["gpm"] == 400.0             # 12000 / (1800/60)
    assert o["dpm"] == 500.0
    assert o["gold_pct"] == 24.0
    assert o["dmg_pct"] == 28.0


def test_at15_excludes_partial_games(client):
    """The partial-data Renata game has null at-15 -> averaged over 3 games."""
    o = client.get("/api/player/Zeus/stats",
                   params={"season": 15, "split": "Spring"}).json()["overall"]
    assert o["gd15"] == 300.0
    assert o["csd15"] == 5.0


def test_lck_role_baseline(client):
    d = client.get("/api/player/Zeus/stats",
                   params={"season": 15, "split": "Spring"}).json()
    base = d["lck_role_baseline"]
    # all LCK top kills in 2025 Spring: [4,3,2,0,2,1] -> 2.0
    assert base["kills"] == 2.0


def test_champion_list_and_images(client):
    d = client.get("/api/player/Zeus/stats",
                   params={"season": 15, "split": "Spring"}).json()
    champs = {c["champion"]: c for c in d["champions"]}
    assert set(champs) == {"Jax", "Gnar", "Renata Glasc"}
    assert champs["Jax"]["games"] == 2
    # sorted by games desc
    assert d["champions"][0]["champion"] == "Jax"
    # Renata Glasc normalizes to the Data Dragon id "Renata"
    assert champs["Renata Glasc"]["champion_ddragon"] == "Renata"
    assert champs["Renata Glasc"]["image_url"].endswith("/Renata.png")


def test_champion_detail_three_way_comparison(client):
    d = client.get("/api/player/Zeus/stats",
                   params={"season": 15, "split": "Spring", "champion": "Jax"}).json()
    sc = d["selected_champion"]
    assert sc["champion"] == "Jax" and sc["games"] == 2
    assert sc["kills"] == 3.5                         # (4+3)/2
    # LCK Jax (top) baseline kills: Zeus 4,3 + Kiin 2 -> 3.0
    assert d["lck_champion_baseline"]["kills"] == 3.0
    # player overall still present for the third comparison column
    assert d["overall"]["games"] == 4


def test_champion_tiers(client):
    """Kiin 2026 Spring: Kennen is top, Gragas is bottom, Sion (2 games) untiered."""
    d = client.get("/api/player/Kiin/stats",
                   params={"season": 16, "split": "Spring"}).json()
    tiers = {c["champion"]: c["tier"] for c in d["champions"]}
    assert tiers["Kennen"] == "top"
    assert tiers["Gragas"] == "bottom"
    assert tiers["Sion"] is None          # strong stats but only 2 games


def test_tier_requires_min_three_games(client):
    """Zeus 2025 Spring Jax (2 games) is never tiered regardless of stats."""
    d = client.get("/api/player/Zeus/stats",
                   params={"season": 15, "split": "Spring"}).json()
    jax = next(c for c in d["champions"] if c["champion"] == "Jax")
    assert jax["games"] == 2 and jax["tier"] is None


def test_win_streak_detected(client):
    """Kiin's most recent 3 games (by gameid order) are all wins."""
    d = client.get("/api/player/Kiin/stats",
                   params={"season": 16, "split": "Spring"}).json()
    assert d["streak"] == {"type": "win", "length": 3}


def test_no_streak_below_threshold(client):
    """Ruler has a single seeded game -> no 3+ streak."""
    d = client.get("/api/player/Ruler/stats",
                   params={"season": 15, "split": "Spring"}).json()
    assert d["streak"] is None


def test_empty_timeframe_is_graceful(client):
    d = client.get("/api/player/Zeus/stats",
                   params={"season": 15, "split": "Summer"}).json()
    assert d["overall"]["games"] == 0
    assert d["champions"] == []


def test_frontend_served_with_no_cache(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "LCK Pro Player Stats" in r.text
    assert "no-store" in r.headers.get("cache-control", "")
