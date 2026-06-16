"""Thin client for the (unofficial, public) lolesports API + livestats feed.

Used only by the offline ``backend.enrich_lolesports`` backfill — the live app
never calls this. Two hosts are involved:

* ``esports-api.lolesports.com`` — schedule / event details (which games exist).
* ``feed.lolesports.com/livestats`` — per-frame game data (items, level, gold…).

Auth is a single public ``x-api-key`` that lolesports.com itself ships in the
browser. There is no documented rate limit, so we throttle politely and retry a
couple of times on transient failures. This is a best-effort source: callers
should treat any failure as "data unavailable", not fatal.
"""
import datetime as dt
import json
import logging
import random
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# Public key shipped by lolesports.com (see project notes / API docs).
API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"
ESPORTS_API = "https://esports-api.lolesports.com/persisted/gw"
LIVESTATS = "https://feed.lolesports.com/livestats/v1"
HL = "en-US"

# Discovered once via getLeagues; stable. Kept as a constant to save a call.
LCK_LEAGUE_ID = "98767991310872058"

# Oracle position codes ← lolesports livestats role names.
ROLE_TO_POSITION = {
    "top": "top", "jungle": "jng", "mid": "mid", "bottom": "bot", "support": "sup",
}

_THROTTLE_S = 0.05           # base polite delay (jittered) — small since callers run concurrently
_RETRIES = 3


def _get(url: str, *, params: dict | None = None) -> dict:
    """GET + parse JSON with the api key header, retrying transient errors."""
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    last_exc: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
            with urllib.request.urlopen(req, timeout=20) as resp:
                time.sleep(_THROTTLE_S + random.uniform(0, _THROTTLE_S))
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            # 400 = our request is malformed (e.g. mis-aligned startingTime); don't
            # retry those. 5xx/429 are worth a backoff.
            if exc.code in (400, 404):
                raise
            last_exc = exc
            log.warning("HTTP %s on %s (attempt %d/%d)", exc.code, url, attempt, _RETRIES)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            log.warning("network error on %s (attempt %d/%d): %s", url, attempt, _RETRIES, exc)
        time.sleep(0.5 * attempt)
    raise RuntimeError(f"GET failed after {_RETRIES} attempts: {url}") from last_exc


# ── Schedule / event discovery (esports-api) ─────────────────────────────────

def iter_completed_matches(league_id: str = LCK_LEAGUE_ID):
    """Yield ``(start_time, match_id)`` for completed matches, newest→oldest.

    Walks ``getSchedule`` backwards via the ``pages.older`` cursor until exhausted.
    """
    token: str | None = None
    seen_pages = 0
    while True:
        params = {"hl": HL, "leagueId": league_id}
        if token:
            params["pageToken"] = token
        sched = _get(f"{ESPORTS_API}/getSchedule", params=params)["data"]["schedule"]
        for ev in sched.get("events", []):
            if ev.get("state") == "completed" and ev.get("match"):
                yield ev.get("startTime"), ev["match"]["id"]
        token = sched.get("pages", {}).get("older")
        seen_pages += 1
        if not token:
            log.info("schedule exhausted after %d pages", seen_pages)
            return


def event_games(match_id: str) -> list[dict]:
    """Return ``[{id, number, state}]`` for the games of a match (best-of-N)."""
    ev = _get(f"{ESPORTS_API}/getEventDetails", params={"hl": HL, "id": match_id})
    games = ev.get("data", {}).get("event", {}).get("match", {}).get("games", [])
    return [{"id": g["id"], "number": g.get("number"), "state": g.get("state")} for g in games]


# ── Livestats feed ───────────────────────────────────────────────────────────

def _floor_10s(t: dt.datetime) -> dt.datetime:
    """The livestats feed only accepts startingTime on a 10-second boundary."""
    t = t.astimezone(dt.timezone.utc).replace(microsecond=0)
    return t - dt.timedelta(seconds=t.second % 10)


def _parse_ts(ts: str) -> dt.datetime:
    """Parse an rfc460 timestamp (``...Z`` / fractional seconds) to aware UTC."""
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def window_metadata(game_id: str | int) -> dict | None:
    """Return lineup/metadata + the game's first-frame time, or None if no data.

    Shape::

        {"esports_game_id", "patch", "start": datetime,
         "sides": {"Blue": [champ_key…], "Red": [champ_key…]},
         "players": {"Blue": [{"role","position","champion","summoner"}…], "Red": […]}}
    """
    try:
        w = _get(f"{LIVESTATS}/window/{game_id}")
    except urllib.error.HTTPError as exc:
        log.warning("window %s unavailable: HTTP %s", game_id, exc.code)
        return None
    frames = w.get("frames") or []
    gm = w.get("gameMetadata") or {}
    if not frames or not gm:
        return None

    sides: dict[str, list[str]] = {}
    players: dict[str, list[dict]] = {}
    for side, key in (("Blue", "blueTeamMetadata"), ("Red", "redTeamMetadata")):
        meta = gm.get(key, {}).get("participantMetadata", [])
        sides[side] = [p.get("championId") for p in meta]
        players[side] = [{
            "participant_id": p.get("participantId"),
            "role": p.get("role"),
            "position": ROLE_TO_POSITION.get(p.get("role"), p.get("role")),
            "champion": p.get("championId"),
            "summoner": p.get("summonerName"),
        } for p in meta]

    return {
        "esports_game_id": str(w.get("esportsGameId") or game_id),
        "patch": gm.get("patchVersion"),
        "start": _parse_ts(frames[0]["rfc460Timestamp"]),
        "sides": sides,
        "players": players,
    }


def details_at(game_id: str | int, when: dt.datetime) -> dict | None:
    """One ``/details`` snapshot at/after ``when`` → ``{ts, participants}`` or None.

    A single ``/details`` call returns ~48 frames spanning only ~10s of game time, so
    this is effectively one snapshot. Callers sample at coarse intervals (e.g. every
    90s) rather than paging densely — see ``enrich_lolesports.enrich_game``. Returns
    ``None`` once ``when`` is past the end of the game (empty response / HTTP 400).
    """
    starting = _floor_10s(when).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        d = _get(f"{LIVESTATS}/details/{game_id}", params={"startingTime": starting})
    except urllib.error.HTTPError as exc:
        log.debug("details %s @ %s: HTTP %s", game_id, starting, exc.code)
        return None
    frames = d.get("frames") or []
    if not frames:
        return None
    f = frames[0]
    return {"ts": f.get("rfc460Timestamp"), "participants": f.get("participants", [])}
