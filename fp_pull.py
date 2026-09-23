"""Phase 2b: pull FantasyPros projections/rankings/player map into DuckDB.

Every API response is cached in fp_raw; a URL is only re-fetched when its cache
row is older than --max-age-hours (default 20h => ~once a day). Run with --force
before lineups lock. The Streamlit app should only ever read the fp_* tables.

    uv run python fp_pull.py --week 4
    uv run python fp_pull.py --week 4 --force
"""
import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
import duckdb
import pandas as pd
from dotenv import load_dotenv

BASE = "https://api.fantasypros.com/public/v2/json"
DB = "fantasy.duckdb"
SEASON = 2026
POSITIONS = ["QB", "RB", "WR", "TE"]

# League rules. FP projections are raw stats (scoring=STD), so we score them ourselves.
SCORING = {
    "pass_yds": 0.04, "pass_tds": 4, "pass_ints": -2,
    "rush_yds": 0.1, "rush_tds": 6,
    "rec_rec": 1, "rec_yds": 0.1, "rec_tds": 6,
    "fumbles": -2, "ret_tds": 6, "2pt_tds": 2,
}


def api_key():
    load_dotenv(".env")
    if key := os.environ.get("FANTASYPROS_API_KEY"):
        return key
    raise SystemExit("Set FANTASYPROS_API_KEY in env or .env")


def fetch(con, path, params, max_age, force):
    """GET with a DuckDB-backed cache keyed on the full URL."""
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    row = con.execute("SELECT fetched_at, body FROM fp_raw WHERE url = ?", [url]).fetchone()
    now = datetime.now(timezone.utc)
    if row and not force and now - row[0] < max_age:
        return json.loads(row[1])
    req = urllib.request.Request(url, headers={"x-api-key": api_key(), "Accept": "application/json",
                                                "User-Agent": "Mozilla/5.0 fantasyeval"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
    con.execute("INSERT OR REPLACE INTO fp_raw VALUES (?, ?, ?)", [url, now, body])
    print(f"fetched {url}")
    data = json.loads(body)
    # Free tier caps every response at 10 players regardless of `count`.
    if len(data.get("players", [])) < int(data.get("count") or 0):
        print(f"  WARNING: truncated, got {len(data['players'])} of {data['count']} players")
    return data


def score(stats):
    return round(sum(float(stats.get(k) or 0) * w for k, w in SCORING.items()), 2)


def espn_id(p):
    # ponytail: spec doesn't show where external IDs land; covers flat and nested shapes.
    v = p.get("espn_id") or (p.get("external_ids") or {}).get("espn")
    return str(v) if v not in (None, "", 0, "0") else None


def replace(con, table, df, where):
    """Replace the slice of `table` matching `where` with df (creates table on first run)."""
    con.register("df", df)
    if con.execute(f"SELECT count(*) FROM information_schema.tables WHERE table_name = '{table}'").fetchone()[0]:
        con.execute(f"DELETE FROM {table} WHERE {where}")
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM df")
    else:
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM df")
    con.unregister("df")


def pull(week, max_age, force):
    con = duckdb.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS fp_raw (url TEXT PRIMARY KEY, fetched_at TIMESTAMPTZ, body TEXT)")
    kw = dict(max_age=max_age, force=force)

    # Player map: FP id <-> ESPN id. Everything joins through this.
    players = fetch(con, "/nfl/players", {"external_ids": "espn"}, **kw)["players"]
    pmap = pd.DataFrame([{
        "fp_id": str(p["player_id"]), "espn_id": espn_id(p), "name": p["player_name"],
        "position": p["position_id"], "team": p["team_id"],
    } for p in players])
    replace(con, "fp_player_map", pmap, "true")

    # Projections: weekly + rest of season, raw stats + league points.
    rows = []
    for pos in POSITIONS:
        for ros in (False, True):
            params = {"position": pos, "week": week} | ({"ros": "true"} if ros else {})
            for p in fetch(con, f"/nfl/{SEASON}/projections", params, **kw)["players"]:
                stats = p["stats"][0] if isinstance(p["stats"], list) else p["stats"]
                rows.append({"fp_id": str(p["fpid"]), "week": week, "ros": ros, "position": pos,
                             "fp_points": score(stats), **stats})
    replace(con, "fp_projections", pd.DataFrame(rows), f"week = {week}")

    # Consensus rankings (PPR): weekly + ROS, kept as flat raw columns.
    frames = []
    for pos in POSITIONS:
        for rtype, wk in (("WEEKLY", week), ("ROS", 0)):
            params = {"position": pos, "scoring": "PPR", "week": wk} | ({"type": "ROS"} if rtype == "ROS" else {})
            ranked = fetch(con, f"/nfl/{SEASON}/consensus-rankings", params, **kw)["players"]
            df = pd.json_normalize(ranked).assign(week=week, rank_type=rtype, position=pos)
            frames.append(df.rename(columns={"player_id": "fp_id"}).astype({"fp_id": str}))
    replace(con, "fp_rankings", pd.concat(frames, ignore_index=True), f"week = {week}")

    unmapped = con.execute("SELECT count(*) FROM fp_player_map WHERE espn_id IS NULL").fetchone()[0]
    print(f"done: {len(pmap)} players ({unmapped} without ESPN id), {len(rows)} projection rows")


if __name__ == "__main__":
    # self-check: a QB with 4000 yds / 30 TD / 10 INT / 200 rush yds / 2 rush TD
    assert score({"pass_yds": 4000, "pass_tds": 30, "pass_ints": 10, "rush_yds": 200, "rush_tds": 2}) == 292
    assert score({"rec_rec": 100, "rec_yds": 1200, "rec_tds": 10}) == 280  # PPR WR
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, required=True, help="0 = preseason")
    ap.add_argument("--max-age-hours", type=float, default=20)
    ap.add_argument("--force", action="store_true", help="ignore cache (run before lineups lock)")
    a = ap.parse_args()
    pull(a.week, timedelta(hours=a.max_age_hours), a.force)
