import pytest

from value import replacement_levels

POOL = [("QB", 25), ("QB", 20), ("QB", 10),
        ("RB", 20), ("RB", 15), ("RB", 10), ("RB", 5),
        ("WR", 18), ("WR", 12), ("WR", 11), ("WR", 4)]


def test_dedicated_plus_flex_split_by_where_value_lands():
    # 2 teams: dedicated RB 20,15 / WR 18,12; 2 flex spots go to WR 11 and RB 10.
    r = replacement_levels(POOL, {"QB": 1, "RB": 1, "WR": 1, "RB/WR/TE": 1, "BE": 6}, teams=2)
    assert r == {"QB": 10, "RB": 5, "WR": 4}


def test_flex_goes_all_to_one_position_when_it_is_deeper():
    pool = [("RB", 20), ("RB", 19), ("RB", 18), ("RB", 17), ("WR", 10), ("WR", 3), ("WR", 2)]
    r = replacement_levels(pool, {"RB": 1, "WR": 1, "RB/WR/TE": 1}, teams=2)
    # dedicated RB 20,19 / WR 10,3; flex takes RB 18, RB 17 -> RB exhausted (0), WR 2
    assert r == {"RB": 0.0, "WR": 2}


def test_narrow_flex_filled_before_wide():
    pool = [("QB", 30), ("QB", 29), ("RB", 10), ("RB", 9), ("WR", 8)]
    # 1 team, RB/WR flex filled first (RB 10), then OP takes QB 29 over RB 9
    r = replacement_levels(pool, {"QB": 1, "RB/WR": 1, "OP": 1}, teams=1)
    assert r == {"QB": 0.0, "RB": 9, "WR": 8}


def test_ignores_bench_and_missing_positions():
    r = replacement_levels([("TE", 9), ("TE", 5)], {"TE": 1, "QB": 1, "BE": 7, "IR": 1}, teams=1)
    assert r == {"TE": 5}
