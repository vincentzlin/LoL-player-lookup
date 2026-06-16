# LCK Pro Player Stats

A query form to search four LCK pros — **Teddy, Ruler, Kiin, Zeus** — and explore
their **pro-play** statistics, filterable by season (S14/S15/S16 = 2024/2025/2026)
and split, broken down per champion (clickable champion-image grid), and compared
against the **LCK role average** for the same timeframe.

Stats shown (pro play only): avg K/D/A per game, CS/min, gold/min, damage/min,
gold%, damage%, CS diff @15, gold diff @15.

## Stack
FastAPI + SQLite (SQLAlchemy) backend, vanilla JS/HTML/CSS frontend. Champion
images from Riot Data Dragon.

## Setup

1. Install deps:
   ```
   pip install -r requirements.txt
   ```

2. **Download the data.** Get the Oracle's Elixir yearly match-data CSVs from
   <https://oracleselixir.com/tools/downloads> — the files named
   `2024_LoL_esports_match_data_from_OraclesElixir.csv`,
   `2025_..._OraclesElixir.csv`, and `2026_..._OraclesElixir.csv`.
   Drop them into `data/raw/`.

3. Load them into the database (LCK rows only; auto-discovers CSVs in `data/raw/`):
   ```
   python -m backend.load_data
   ```
   It logs how many rows each pro matched — confirm Teddy/Ruler/Kiin/Zeus show `[OK]`.

4. *(Optional)* **Enrich with item timing + champion level.** Oracle's CSVs have
   neither, so the app shows "N/A" for both until you backfill from the (unofficial,
   public) lolesports livestats feed. This is a **long, resumable background job**
   (~1–2h for the full ~1400 LCK games) — it writes to the DB as it goes, so the site
   updates progressively, and it's safe to Ctrl-C and re-run (it resumes from the
   `data/lolesports_*.json` caches):
   ```
   python -m backend.enrich_lolesports          # full backfill (resumable)
   python -m backend.enrich_lolesports --max-games 50   # a quick partial slice
   ```
   It resolves each stored game to its lolesports game by champion-lineup + date, then
   fills `item1/2/3_completed_s` (sampled, so timing is accurate to ~±90s) and `level`.
   Run it **after** `load_data` (which rebuilds the table). Probe one game without
   touching the DB:
   ```
   python -m backend.enrich_lolesports --probe 115548128963037588
   ```

5. Run the app:
   ```
   python run.py
   ```
   Open <http://127.0.0.1:8000>.

## Notes
- Only **LCK** games are included. Ruler's 2024 LPL (JDG) season is therefore
  excluded, so his season filter only offers the years he has LCK games.
- At-15 differentials and gold/damage shares come from games Oracle marks
  `complete`; partial-data games still count toward game totals but are skipped
  when averaging those specific fields.
- To refresh data later, re-download the CSVs and re-run `python -m backend.load_data`
  (it rebuilds the table from scratch each run), then re-run
  `python -m backend.enrich_lolesports` to repopulate item timing + level. The
  `data/lolesports_*.json` caches mean this only re-writes the DB (no re-fetching),
  so it's quick the second time.
- **Item timing / level are best-effort**: they come from an unofficial feed and are
  only filled for games whose lineup+date could be matched; unmatched games stay "N/A".
  Item timing is **sampled** (~±90s), not exact-to-the-second.
