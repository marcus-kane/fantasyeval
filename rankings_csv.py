"""Load manually exported rankings CSVs (FantasyPros ROS, Flock) into the `rankings` table.

Drop a fresh export in the project folder and rerun; the newest file per source wins.
CSVs carry names, not IDs, so players are matched to player_ids on a normalized
name + position (same scheme as DynastyProcess merge_name). Unmatched rows are printed.

    uv run python rankings_csv.py
"""
import glob
import os

import duckdb

from fp_pull import DB, load_player_ids

# source -> (file glob, SELECT producing rank/name/team/position/pos_rank/tier from `src`)
SOURCES = {
    "fantasypros": ("FantasyPros_*_ALL_Rankings.csv", """
        SELECT "RK"::INT AS rank, "PLAYER NAME" AS name, TEAM AS team,
               regexp_extract("POS", '^[A-Z]+') AS position,
               nullif(regexp_extract("POS", '\\d+$'), '')::INT AS pos_rank, NULL AS tier FROM src WHERE "RK" IS NOT NULL AND "RK" <> ''"""),
    "flock": ("FlockRankings*.csv", """
        SELECT "Rank"::INT AS rank, "Name" AS name, "Team" AS team, "Position" AS position,
               (row_number() OVER (PARTITION BY "Position" ORDER BY "Rank"::INT))::INT AS pos_rank,
               "Tier" AS tier FROM src WHERE "Rank" IS NOT NULL"""),
}

# Mirrors DynastyProcess merge_name: lowercase, drop suffixes and punctuation (hyphens kept).
NORM = r"""trim(regexp_replace(regexp_replace(lower({}), '\b(jr|sr|ii|iii|iv|v)\b\.?', '', 'g'), '[^a-z -]', '', 'g'))"""

# Nicknames that don't share a first initial with the player_ids name. Add as they show up unmatched.
ALIASES = {"hollywood brown": "marquise brown"}


def load():
    con = duckdb.connect(DB)
    load_player_ids(con)
    frames = []
    for source, (pattern, select) in SOURCES.items():
        files = sorted(glob.glob(pattern), key=os.path.getmtime)
        if not files:
            print(f"{source}: no file matching {pattern}, skipped")
            continue
        con.execute(f"CREATE OR REPLACE TEMP VIEW src AS SELECT * FROM read_csv('{files[-1]}', all_varchar=true, header=true, quote='\"', null_padding=true, strict_mode=false)")
        con.execute(f"CREATE OR REPLACE TEMP TABLE r_{source} AS SELECT '{source}' AS source, '{files[-1]}' AS file, * FROM ({select})")
        frames.append(f"r_{source}")
    alias = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in ALIASES.items())
    con.execute(f"""
        CREATE OR REPLACE TABLE rankings AS
        WITH r AS (
            SELECT *, CASE {NORM.format('name')} {alias} ELSE {NORM.format('name')} END AS key
            FROM ({' UNION ALL BY NAME '.join(f'SELECT * FROM {f}' for f in frames)})),
        ids AS (SELECT *, left(merge_name, 1) || ' ' || split_part(merge_name, ' ', -1) AS short_key
                FROM player_ids WHERE espn_id IS NOT NULL),
        -- fallback key: first initial + last name + position (Kenny/Kenneth, Chig/Chigoziem), only if unique
        short AS (SELECT * FROM ids QUALIFY count(*) OVER (PARTITION BY short_key, position) = 1)
        SELECT r.* EXCLUDE (key), coalesce(e.fantasypros_id, s.fantasypros_id) AS fp_id,
               coalesce(e.espn_id, s.espn_id) AS espn_id, coalesce(e.gsis_id, s.gsis_id) AS gsis_id,
               now() AS loaded_at
        FROM r
        LEFT JOIN ids e ON e.merge_name = r.key AND e.position = r.position
        LEFT JOIN short s ON e.espn_id IS NULL AND s.position = r.position
             AND s.short_key = left(r.key, 1) || ' ' || split_part(r.key, ' ', -1)
        QUALIFY row_number() OVER (PARTITION BY r.source, r.rank, r.name) = 1""")
    print(con.sql("SELECT source, count(*) n, count(espn_id) AS mapped FROM rankings GROUP BY 1"))
    print(con.sql("""SELECT source, rank, name, team, position FROM rankings
                     WHERE espn_id IS NULL AND position NOT IN ('K', 'DST') ORDER BY source, rank"""))


if __name__ == "__main__":
    load()
