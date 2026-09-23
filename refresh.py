"""Phase 2: one entrypoint that pulls everything into DuckDB.

    uv run python refresh.py            # ESPN (all leagues) + FantasyPros + CSV rankings
    uv run python refresh.py --espn     # ESPN only

ESPN tables are rebuilt wholesale each run (ESPN keeps full history), all keyed by
league_id and stamped with pulled_at. User-owned tables (overrides, Phase 3) are
never touched here.

Tables
  espn_leagues      one row per league: name, sizes, current week, deadline, my_team_id
  espn_slots        starting/bench slot counts per league (from league.settings)
  espn_scoring      scoring rules per league (stat id, abbr, points)
  espn_teams        standings + owners, is_me flag
  espn_players      rostered players + top free agents (team_id NULL), ESPN projections
  espn_player_weeks per-player weekly actual + projected points (rostered players)
  espn_lineups      weekly box-score lineups: who was in which slot and what they scored
  espn_schedule     full season schedule per team (future weeks have NULL scores)
"""
import argparse
import os
import urllib.error
from datetime import datetime, timedelta, timezone

import duckdb
import polars as pl
from dotenv import load_dotenv
from espn_api.football import League
from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError

import fp_pull
import rankings_csv
import value
from auth import AuthError

DB = fp_pull.DB
FREE_AGENTS = 150  # waiver pool depth, enough to set replacement level


def connect_league(league_id, year, s2, swid):
    try:
        lg = League(league_id=league_id, year=year, espn_s2=s2, swid=swid)
    except (ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError) as e:
        raise AuthError(f"League {league_id}: {e!r}. Rerun `uv run python auth.py`.") from e
    if not any(t.roster for t in lg.teams):
        raise AuthError(f"League {league_id}: every roster is empty. Cookies are likely stale; rerun `uv run python auth.py`.")
    return lg


def player_row(p, league_id, team_id, weeks=range(0)):
    # espn-api uses [] for missing scalars on free agents (injuryStatus, etc.)
    return {k: None if v == [] and k != "eligible_slots" else v for k, v in {
        "league_id": league_id, "team_id": team_id, "espn_id": str(p.playerId), "name": p.name,
        "position": p.position, "pro_team": p.proTeam, "lineup_slot": p.lineupSlot if team_id else None,
        "eligible_slots": list(p.eligibleSlots), "injury_status": p.injuryStatus, "injured": p.injured,
        "acquisition_type": p.acquisitionType or None, "percent_owned": p.percent_owned,
        "percent_started": p.percent_started, "avg_points": p.avg_points, "total_points": p.total_points,
        "projected_avg_points": p.projected_avg_points, "projected_total_points": p.projected_total_points,
        "games_remaining": sum(1 for wk in (p.schedule or {}) if int(wk) in weeks),  # byes excluded
    }.items()}


def is_me(team, swid):
    return any((o.get("id") if isinstance(o, dict) else o) == swid for o in team.owners or [])


def league_rows(lg, league_id, swid):
    """Flatten one espn-api League into {table: [rows]}."""
    s = lg.settings
    me = next((t.team_id for t in lg.teams if is_me(t, swid)), None)
    last_week = max(max(v) for v in s.matchup_periods.values())  # fantasy season incl. playoffs
    weeks = range(lg.current_week, last_week + 1)
    out = {k: [] for k in ("espn_leagues", "espn_slots", "espn_scoring", "espn_teams", "espn_players",
                           "espn_player_weeks", "espn_lineups", "espn_schedule")}
    out["espn_leagues"].append({
        "league_id": league_id, "name": s.name, "year": lg.year, "team_count": s.team_count,
        "current_week": lg.current_week, "reg_season_count": s.reg_season_count,
        "playoff_team_count": s.playoff_team_count, "scoring_type": s.scoring_type,
        "trade_deadline": datetime.fromtimestamp(s.trade_deadline / 1000, timezone.utc) if s.trade_deadline else None,
        "last_week": last_week, "my_team_id": me,
    })
    out["espn_slots"] += [{"league_id": league_id, "slot": k, "count": v}
                          for k, v in s.position_slot_counts.items() if v]
    out["espn_scoring"] += [{"league_id": league_id, "stat_id": r["id"], "abbr": r["abbr"],
                             "label": r["label"], "points": r["points"]} for r in s.scoring_format]
    for t in lg.teams:
        out["espn_teams"].append({
            "league_id": league_id, "team_id": t.team_id, "team_name": t.team_name, "abbrev": t.team_abbrev,
            "owners": ", ".join(o.get("displayName", "") if isinstance(o, dict) else str(o) for o in t.owners or []),
            "is_me": t.team_id == me, "wins": t.wins, "losses": t.losses, "ties": t.ties,
            "points_for": t.points_for, "points_against": t.points_against, "standing": t.standing,
        })
        for p in t.roster:
            out["espn_players"].append(player_row(p, league_id, t.team_id, weeks))
            out["espn_player_weeks"] += [
                {"league_id": league_id, "espn_id": str(p.playerId), "week": wk,
                 "points": st.get("points"), "projected_points": st.get("projected_points")}
                for wk, st in p.stats.items() if wk]  # week 0 = season totals
        for wk, (opp, score, outcome) in enumerate(zip(t.schedule, t.scores, t.outcomes), start=1):
            out["espn_schedule"].append({
                "league_id": league_id, "week": wk, "team_id": t.team_id, "opp_team_id": opp.team_id,
                "score": score if outcome != "U" else None, "outcome": outcome})
    out["espn_players"] += [player_row(p, league_id, None, weeks) for p in lg.free_agents(size=FREE_AGENTS)]
    for wk in range(1, lg.current_week + 1):
        for bs in lg.box_scores(wk):
            for team, lineup in ((bs.home_team, bs.home_lineup), (bs.away_team, bs.away_lineup)):
                if not team:  # bye
                    continue
                out["espn_lineups"] += [
                    {"league_id": league_id, "week": wk, "team_id": team.team_id, "espn_id": str(p.playerId),
                     "name": p.name, "position": p.position, "slot": p.slot_position,
                     "points": p.points, "projected_points": p.projected_points}
                    for p in lineup]
    return out


def refresh_espn(con):
    load_dotenv(".env", override=True)
    year, s2, swid = int(os.environ["YEAR"]), os.environ["ESPN_S2"], os.environ["SWID"]
    tables, week = {}, None
    for league_id in (int(x) for x in os.environ["LEAGUE_ID"].split(",")):
        lg = connect_league(league_id, year, s2, swid)
        week = lg.current_week
        for name, rows in league_rows(lg, league_id, swid).items():
            tables.setdefault(name, []).extend(rows)
        print(f"ESPN {league_id} ({lg.settings.name}): {len(lg.teams)} teams, week {week}")
    pulled_at = datetime.now(timezone.utc)
    for name, rows in tables.items():
        df = pl.from_dicts(rows, infer_schema_length=None).with_columns(pulled_at=pl.lit(pulled_at))
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM df")
        print(f"  {name}: {len(df)} rows")
    return week


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--espn", action="store_true", help="ESPN only; skip FantasyPros and CSV rankings")
    ap.add_argument("--force", action="store_true", help="ignore FantasyPros cache (before lineups lock)")
    a = ap.parse_args()
    with duckdb.connect(DB) as con:
        week = refresh_espn(con)
    if not a.espn:
        try:
            fp_pull.pull(week, timedelta(hours=20), a.force)
        except urllib.error.HTTPError as e:
            # FP free tier has an unpublished quota; cached batches survive for the next run.
            print(f"WARNING: FantasyPros pull stopped ({e.code} {e.reason}); fp_projections is partial or stale.")
        rankings_csv.load()
    with duckdb.connect(DB) as con:
        print(f"player_values: {len(value.compute(con))} rows")


if __name__ == "__main__":
    main()
