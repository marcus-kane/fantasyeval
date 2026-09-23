from types import SimpleNamespace

import pytest

from auth import AuthError
from refresh import connect_league, is_me, player_row

SWID = "{ABC}"


def test_is_me_matches_swid_owner():
    assert is_me(SimpleNamespace(owners=[{"id": "{XYZ}"}, {"id": SWID}]), SWID)
    assert not is_me(SimpleNamespace(owners=[{"id": "{XYZ}"}]), SWID)
    assert not is_me(SimpleNamespace(owners=None), SWID)


def test_player_row_blanks_empty_lists_but_keeps_slots():
    p = SimpleNamespace(playerId=1, name="X", position="WR", proTeam="DET", lineupSlot="WR",
                        eligibleSlots=[], injuryStatus=[], injured=False, acquisitionType=[],
                        percent_owned=1.0, percent_started=1.0, avg_points=0, total_points=0,
                        projected_avg_points=0, projected_total_points=0)
    row = player_row(p, 1, None)
    assert row["injury_status"] is None and row["eligible_slots"] == [] and row["lineup_slot"] is None


def test_empty_rosters_fail_loudly(monkeypatch):
    fake = SimpleNamespace(teams=[SimpleNamespace(roster=[]), SimpleNamespace(roster=[])])
    monkeypatch.setattr("refresh.League", lambda **kw: fake)
    with pytest.raises(AuthError, match="empty"):
        connect_league(1, 2026, "s2", SWID)
