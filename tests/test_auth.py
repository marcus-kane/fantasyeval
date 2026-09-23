import pytest

from auth import AuthError, check_league


def test_no_teams_fails():
    with pytest.raises(AuthError, match="no teams"):
        check_league({"teams": []}, "1")


def test_all_empty_rosters_fails():
    data = {"teams": [{"roster": {"entries": []}}, {"roster": {}}]}
    with pytest.raises(AuthError, match="empty"):
        check_league(data, "1")


def test_populated_league_passes():
    data = {"teams": [{"roster": {"entries": [{"playerId": 1}]}}, {"roster": {"entries": []}}]}
    assert check_league(data, "1") == 2
