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
| `auth.py` | Playwright persistent profile â†’ espn_s2/SWID into `.env`; verifies each league, fails loudly on 401/empty rosters |
| `refresh.py` | Single entrypoint. ESPN (espn-api) â†’ `espn_*` tables for all leagues, then `fp_pull`, then `rankings_csv` |
| `fp_pull.py` | FantasyPros projections â†’ `fp_projections`, raw responses cached in `fp_raw`; loads `player_ids` |
| `rankings_csv.py` | Manually exported FantasyPros ROS + Flock CSVs â†’ `rankings` |

## Decisions
- **IDs:** everything joins on ESPN id via `player_ids` (DynastyProcess `db_playerids.csv`: FP, ESPN, gsis/nflverse, sleeper...). Never join on names, except the CSV rankings, which only carry names (normalized name + position, first-initial fallback, `ALIASES` for nicknames).
- **Roster slots and scoring come from ESPN** (`espn_slots`, `espn_scoring`), never hardcoded. Leagues differ (bench 6 vs 7).
- **`espn-api` is pinned** (unofficial, breaks). Bump deliberately.
- **ESPN tables are rebuilt each refresh.** User-owned tables (overrides, notes) must live in separate tables that refresh never touches.
- **FantasyPros free tier:** every response is capped at 10 players (use the `players=` filter, 10 ids per call), with an unpublished quota of roughly 45-50 calls/day (429). Only ESPN's 350 most-owned QB/RB/WR/TE are requested. A quota hit keeps partial data, and the next run resumes from the cache. FP rankings come from CSV exports, not the API.
- **FP projections are raw stats**, scored by us. TODO: score from `espn_scoring` per league instead of the `SCORING` dict.
- **Routes data is not available (paid).** Use snap share + target share.
- **Replacement level** = best non-starter per position after filling dedicated slots, then flex slots by where the best leftovers are (narrow flexes first). See `value.replacement_levels` docstring.
- **Overrides live in `player_overrides`** (global, not per league) and change projections, never values.
- Phase 4 was built before 2c/3 so trades come sooner; nflverse features will later feed projections.
- Stack: polars + DuckDB (no pandas). Business logic stays out of `app.py`.

## Phases
1 auth âœ… Â· 2 refresh âœ… Â· 2b FantasyPros âœ… Â· 2c nflverse Â· 3 features Â· 4 value (VOR + overrides) Â· 5 trades (lineup-based + win-win finder) Â· 6 deficiencies Â· 7 Streamlit app Â· later: injury comps, backtest
