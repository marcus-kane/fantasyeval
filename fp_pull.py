"""Phase 2b: pull FantasyPros projections + DynastyProcess player ID map into DuckDB.

Every API response is cached in fp_raw; a URL is only re-fetched when its cache
row is older than --max-age-hours (default 20h => ~once a day). Run with --force
before lineups lock. The Streamlit app should only ever read the fp_* tables.

    uv run python fp_pull.py --week 4
    uv run python fp_pull.py --week 4 --force
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd
from dotenv import load_dotenv

BASE = "https://api.fantasypros.com/public/v2/json"
DB = "fantasy.duckdb"
SEASON = 2026
PLAYER_IDS_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"
ESPN_POOL = 350  # ~12-14 teams x ~10 skill players + waiver wire
CALL_GAP_S = 1.0  # FP rate limits are unpublished; raise this if 429s persist

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
    row = con.execute("SELECT epoch(fetched_at), body FROM fp_raw WHERE url = ?", [url]).fetchone()
    now = datetime.now(timezone.utc)
    if row and not force and now.timestamp() - row[0] < max_age.total_seconds():
        return json.loads(row[1])
    req = urllib.request.Request(url, headers={"x-api-key": api_key(), "Accept": "application/json",
                                                "User-Agent": "Mozilla/5.0 fantasyeval"})
    # AWS API Gateway throttles bursts (429); pace calls and back off.
    for attempt in range(5):
        time.sleep(CALL_GAP_S * 2 ** attempt)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode()
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise
    con.execute("INSERT OR REPLACE INTO fp_raw VALUES (?, ?, ?)", [url, now, body])
    print(f"fetched {url}")
    data = json.loads(body)
    # Free tier caps every response at 10 players regardless of `count`.
    if len(data.get("players", [])) < int(data.get("count") or 0):
        print(f"  WARNING: truncated, got {len(data['players'])} of {data['count']} players")
    return data


def score(stats):
    return round(sum(float(stats.get(k) or 0) * w for k, w in SCORING.items()), 2)


def espn_owned_ids(limit=ESPN_POOL):
    """ESPN ids of the most-owned QB/RB/WR/TE (rostered + realistic waiver adds)."""
    load_dotenv(".env")
    league = os.environ["LEAGUE_ID"].split(",")[0]
    filt = {"players": {"filterSlotIds": {"value": [0, 2, 4, 6]}, "limit": limit,
                        "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
    req = urllib.request.Request(
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}/segments/0/leagues/{league}?view=kona_player_info",
        headers={"Cookie": f"espn_s2={os.environ['ESPN_S2']}; SWID={os.environ['SWID']}",
                 "X-Fantasy-Filter": json.dumps(filt)})
    with urllib.request.urlopen(req, timeout=30) as r:
        ids = [str(p["id"]) for p in json.load(r)["players"]]
    if not ids:
        raise SystemExit("ESPN player pool came back empty; run `uv run python auth.py`.")
    return ids


def load_player_ids(con):
    """Cross-site ID table (FP, ESPN, gsis/nflverse, sleeper...). Everything joins through this."""
    con.execute(f"CREATE OR REPLACE TABLE player_ids AS SELECT * FROM read_csv('{PLAYER_IDS_URL}', all_varchar=true, nullstr='NA')")
    con.execute("""CREATE OR REPLACE VIEW fp_player_map AS SELECT fantasypros_id AS fp_id, espn_id, gsis_id,
                   name, merge_name, position, team FROM player_ids WHERE fantasypros_id IS NOT NULL""")


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

    load_player_ids(con)

    # Free tier returns max 10 players per call, so request the ESPN-owned pool 10 FP ids at a time.
    pool = espn_owned_ids()
    con.register("pool", pd.DataFrame({"espn_id": pool}))
    targets = con.execute("""SELECT position, list(fp_id ORDER BY fp_id) FROM fp_player_map
                             JOIN pool USING (espn_id) WHERE position IN ('QB','RB','WR','TE') GROUP BY 1""").fetchall()
    rows = []
    for pos, ids in targets:
        for i in range(0, len(ids), 10):
            for ros in (False, True):
                params = {"position": pos, "week": week, "players": ":".join(ids[i:i + 10])} | ({"ros": "true"} if ros else {})
                for p in fetch(con, f"/nfl/{SEASON}/projections", params, **kw)["players"]:
                    stats = p["stats"][0] if isinstance(p["stats"], list) else p["stats"]
                    rows.append({"fp_id": str(p["fpid"]), "week": week, "ros": ros, "position": pos,
                                 "fp_points": score(stats), **stats})
    replace(con, "fp_projections", pd.DataFrame(rows), f"week = {week}")
    print(f"done: {len(pool)} ESPN pool players, {sum(len(i) for _, i in targets)} mapped to FP, {len(rows)} projection rows")


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
