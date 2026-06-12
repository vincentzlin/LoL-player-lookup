"""Project configuration for the LoL pro-player stats query app."""
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = str(DATA_DIR / "lol_performance.db")

# ── Scope ────────────────────────────────────────────────────────────────────
# Only LCK pro-play data is loaded and compared.
LEAGUE = "LCK"

# year → in-game "season" number. Last 3 years of play.
SEASONS = {2024: 14, 2025: 15, 2026: 16}

# The only four players that are searchable. `name` must match Oracle's Elixir
# `playername` exactly (verified by the loader). `role` uses Oracle position codes.
# `team` is only a FALLBACK label — the displayed team is the player's most recent
# team derived from the data (see stats.current_team), used only if they have no games.
PLAYERS = [
    {"name": "Teddy", "role": "bot", "team": "BNK FEARX"},
    {"name": "Ruler", "role": "bot", "team": "Gen.G"},
    {"name": "Kiin",  "role": "top", "team": "Gen.G"},
    {"name": "Zeus",  "role": "top", "team": "T1"},
]

# Oracle position code → human label (for the UI).
ROLE_LABELS = {
    "top": "Top",
    "jng": "Jungle",
    "mid": "Mid",
    "bot": "Bot",
    "sup": "Support",
}

# ── Data Dragon (champion square images) ─────────────────────────────────────
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_IMG_TPL = "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{champ}.png"
# Fallback patch used if the version list can't be fetched at startup.
DDRAGON_FALLBACK_VERSION = "15.11.1"


def player_names() -> set[str]:
    return {p["name"] for p in PLAYERS}


def find_player(name: str) -> dict | None:
    """Case-insensitive lookup against the four-player allowlist."""
    low = name.strip().lower()
    for p in PLAYERS:
        if p["name"].lower() == low:
            return p
    return None


def distinct_roles() -> list[str]:
    """Roles represented by the allowlist, ordered top→jng→mid→bot→sup."""
    present = {p["role"] for p in PLAYERS}
    return [r for r in ROLE_LABELS if r in present]
