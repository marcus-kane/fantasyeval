"""Phase 4: value model. Points over replacement (VOR), per league.

    uv run python value.py                                  # recompute player_values
    uv run python value.py override "Chase Brown" --ppg 16 --games 12 --note "OC loves him"
    uv run python value.py override "Chase Brown" --clear

Projection per player (PPG), in priority order:
  1. my override (player_overrides; never touched by refresh)
  2. weighted blend of sources that have the player: ESPN projected_avg_points,
     FantasyPros ROS points / games remaining (see WEIGHTS)
Games remaining: override, else ESPN pro schedule from the current week through the
league's last fantasy week, byes excluded.

Overrides change projections, not values. VOR is always recomputed downstream.
"""
import argparse
from collections import defaultdict

import duckdb
import polars as pl

from fp_pull import DB

WEIGHTS = {"espn": 0.5, "fp": 0.5}
# Flex slot -> eligible positions. Narrower flexes are filled first.
FLEX = {"RB/WR": {"RB", "WR"}, "WR/TE": {"WR", "TE"}, "RB/WR/TE": {"RB", "WR", "TE"},
        "OP": {"QB", "RB", "WR", "TE"}}


def replacement_levels(players, slots, teams):
    """PPG of the replacement-level player at each position.

    players: iterable of (position, ppg) for the league's whole pool (rostered + free agents).
    slots:   {slot: count per team} from espn_slots (bench/IR ignored).
    teams:   number of teams.

    Derivation:
      1. Dedicated slots: the top `slots[pos] * teams` players at each position are starters.
      2. Flex slots: the demand of `slots[flex] * teams` goes to the best *remaining* players
         among the flex's eligible positions, so it splits across RB/WR/TE according to
         where the best non-starters actually are, not by a fixed ratio.
         Narrower flexes (RB/WR) are filled before wider ones (RB/WR/TE, OP).
      3. Replacement level at a position = PPG of the best player at that position who is
         NOT a starter after steps 1-2 (the best one you could pick up or start in a pinch).
         Positions with no leftover players get 0.

    Example: 2 teams, QB/RB/WR/FLEX x1. RB 20,15,10,5; WR 18,12,11,4.
      Dedicated: RB 20,15; WR 18,12. Flex (2 spots): best of RB 10,5 / WR 11,4 -> WR 11, RB 10.
      Replacement: RB 5, WR 4.
    """
    pool = defaultdict(list)
    for pos, ppg in players:
        pool[pos].append(ppg or 0.0)
    for pos in pool:
        pool[pos].sort(reverse=True)
    taken = defaultdict(int)  # starters consumed per position (they're the top N)
    for pos, n in slots.items():
        if pos in pool:
            taken[pos] = min(n * teams, len(pool[pos]))
    for flex in sorted((f for f in slots if f in FLEX), key=lambda f: len(FLEX[f])):
        for _ in range(slots[flex] * teams):
            best = max((p for p in FLEX[flex] if taken[p] < len(pool[p])),
                       key=lambda p: pool[p][taken[p]], default=None)
            if best is None:
                break
            taken[best] += 1
    return {pos: (v[taken[pos]] if taken[pos] < len(v) else 0.0) for pos, v in pool.items() if v}


def ensure_overrides(con):
    con.execute("""CREATE TABLE IF NOT EXISTS player_overrides (
        espn_id TEXT PRIMARY KEY, ppg DOUBLE, games_remaining INTEGER, note TEXT,
        updated_at TIMESTAMPTZ DEFAULT now())""")


def projections(con):
    """One row per (league, player): sources, blended ppg, games."""
    has_fp = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='fp_projections'").fetchone()[0]
    fp_join = """LEFT JOIN player_ids i ON i.espn_id = p.espn_id
                 LEFT JOIN fp_projections f ON f.fp_id = i.fantasypros_id AND f.ros
                      AND f.week = (SELECT max(week) FROM fp_projections)""" if has_fp else ""
    fp_ppg = "f.fp_points / nullif(p.games_remaining, 0)" if has_fp else "NULL::DOUBLE"
    w_e, w_f = WEIGHTS["espn"], WEIGHTS["fp"]
    return con.execute(f"""
        WITH src AS (
            SELECT p.league_id, p.espn_id, p.name, p.position, p.team_id, p.pro_team, p.injury_status,
                   nullif(p.projected_avg_points, 0) AS espn_ppg, {fp_ppg} AS fp_ppg,
                   p.games_remaining AS espn_games, o.ppg AS override_ppg, o.games_remaining AS override_games
            FROM espn_players p {fp_join}
            LEFT JOIN player_overrides o ON o.espn_id = p.espn_id)
        SELECT *,
               coalesce(override_ppg,
                        (coalesce(espn_ppg * {w_e}, 0) + coalesce(fp_ppg * {w_f}, 0))
                        / nullif((espn_ppg IS NOT NULL)::INT * {w_e} + (fp_ppg IS NOT NULL)::INT * {w_f}, 0)
               ) AS ppg,
               coalesce(override_games, espn_games) AS games
        FROM src""").pl()


def compute(con):
    ensure_overrides(con)
    proj = projections(con)
    leagues = con.execute("SELECT league_id, team_count FROM espn_leagues").fetchall()
    out = []
    for league_id, teams in leagues:
        slots = dict(con.execute("SELECT slot, count FROM espn_slots WHERE league_id = ?", [league_id]).fetchall())
        lp = proj.filter(pl.col("league_id") == league_id)
        repl = replacement_levels(lp.select("position", "ppg").iter_rows(), slots, teams)
        out.append(lp.with_columns(
            repl_ppg=pl.col("position").replace_strict(repl, default=0.0, return_dtype=pl.Float64)))
    df = pl.concat(out).with_columns(
        ppg=pl.col("ppg").fill_null(0.0), games=pl.col("games").fill_null(0)).with_columns(
        vor_ppg=pl.col("ppg") - pl.col("repl_ppg")).with_columns(
        vor_ros=pl.col("vor_ppg") * pl.col("games"))
    con.execute("CREATE OR REPLACE TABLE player_values AS SELECT *, now() AS computed_at FROM df")
    return df


def set_override(con, name_or_id, ppg=None, games=None, note=None, clear=False):
    ensure_overrides(con)
    hits = con.execute("""SELECT DISTINCT espn_id, name, position, pro_team FROM espn_players
                          WHERE espn_id = ? OR name ILIKE '%' || ? || '%'""", [name_or_id, name_or_id]).fetchall()
    if len(hits) != 1:
        raise SystemExit(f"'{name_or_id}' matched {len(hits)} players: {hits[:10]}. Use the ESPN id.")
    espn_id = hits[0][0]
    if clear:
        con.execute("DELETE FROM player_overrides WHERE espn_id = ?", [espn_id])
    else:
        con.execute("""INSERT OR REPLACE INTO player_overrides (espn_id, ppg, games_remaining, note)
                       VALUES (?, ?, ?, ?)""", [espn_id, ppg, games, note])
    return hits[0]


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    o = sub.add_parser("override", help="set/clear my projection for a player")
    o.add_argument("player", help="name (substring) or ESPN id")
    o.add_argument("--ppg", type=float)
    o.add_argument("--games", type=int)
    o.add_argument("--note")
    o.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    with duckdb.connect(DB) as con:
        if a.cmd == "override":
            print("override", "cleared" if a.clear else "set", "for", set_override(con, a.player, a.ppg, a.games, a.note, a.clear))
        df = compute(con)
        print(con.sql("""SELECT l.name AS league, v.position, round(any_value(v.repl_ppg), 2) AS repl_ppg
                         FROM player_values v JOIN espn_leagues l USING (league_id)
                         GROUP BY ALL ORDER BY league, position"""))
        print(f"player_values: {len(df)} rows")


if __name__ == "__main__":
    main()
