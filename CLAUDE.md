# fantasyeval

Personal fantasy football tracker for 3 ESPN leagues: value model (VOR on my own rankings), lineup-based trade evaluator, and a win-win trade finder.

## Setup
```
uv sync
uv run playwright install chromium
uv run python auth.py --headed   # once: log in to ESPN by hand; profile saved to ./espn_profile/
uv run python refresh.py         # pulls everything into fantasy.duckdb
uv run pytest -q
```
`.env` (gitignored): `ESPN_S2`, `SWID` (keep the braces), `LEAGUE_ID` (comma-separated), `YEAR`, `FANTASYPROS_API_KEY`.

## Files
| File | Role |
|---|---|
| `auth.py` | Playwright persistent profile -> espn_s2/SWID into `.env`; verifies each league, fails loudly on 401/empty rosters |
| `refresh.py` | Single entrypoint. ESPN (espn-api) -> `espn_*` tables for all leagues, then `fp_pull`, `rankings_csv`, `value` |
| `fp_pull.py` | FantasyPros projections -> `fp_projections`, raw responses cached in `fp_raw`; loads `player_ids` |
| `rankings_csv.py` | Manually exported FantasyPros ROS + Flock CSVs -> `rankings` |
| `value.py` | Blended PPG (override > ESPN/FP blend) x games remaining vs per-league replacement level -> `player_values` (VOR). `value.py override <player> --ppg --games --note` writes `player_overrides` |

## Decisions
- **IDs:** everything joins on ESPN id via `player_ids` (DynastyProcess `db_playerids.csv`: FP, ESPN, gsis/nflverse, sleeper...). Never join on names, except the CSV rankings, which only carry names (normalized name + position, first-initial fallback, `ALIASES` for nicknames).
- **Roster slots and scoring come from ESPN** (`espn_slots`, `espn_scoring`), never hardcoded. Leagues differ (bench 6 vs 7).
- **`espn-api` is pinned** (unofficial, breaks). Bump deliberately.
- **ESPN tables are rebuilt each refresh.** User-owned tables (`player_overrides`, notes) live in separate tables that refresh never touches.
- **FantasyPros free tier:** every response is capped at 10 players (use the `players=` filter, 10 ids per call), with an unpublished quota of roughly 45-50 calls/day (429). Only ESPN's 350 most-owned QB/RB/WR/TE are requested. A quota hit keeps partial data, and the next run resumes from the cache. FP rankings come from CSV exports (gitignored), not the API.
- **FP projections are raw stats**, scored by us. TODO: score from `espn_scoring` per league instead of the `SCORING` dict.
- **Routes data is not available (paid).** Use snap share + target share.
- **Replacement level** = best non-starter per position after filling dedicated slots, then flex slots by where the best leftovers are (narrow flexes first). See the `value.replacement_levels` docstring.
- **Overrides** are global (not per league) and change projections, never values.
- Phase 4 was built before 2c/3 so trades come sooner; nflverse features will later feed projections.
- Stack: polars + DuckDB (no pandas). Business logic stays out of `app.py`.
- Windows PowerShell 5.1 mangles UTF-8 with Get-Content/Set-Content; edit files with the editor tools, not PS string replaces.

## Phases
1 auth (done) / 2 refresh (done) / 2b FantasyPros (done) / 4 value: VOR + overrides (done) / 5 trades: lineup-based + win-win finder / 2c nflverse / 3 features / 6 deficiencies / 7 Streamlit app / later: injury comps, backtest
