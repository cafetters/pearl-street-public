"""M2b step 3a: loading the data-center sessions onto a slice's steps (sim/data.py load_dc). Real-data test skips
without ../data."""
from pathlib import Path

import numpy as np
import pytest

from sim import dc_defer as DC

DATA = Path(__file__).resolve().parents[1].parent / "data"


@pytest.mark.skipif(not (DATA / "dc_sessions_2023.parquet").exists(), reason="needs ../data with the session table")
def test_jan12_sessions_and_workload_line_up():
    from sim.data import load_dc
    ids, g, sessions = load_dc(DATA, "2023-01-12", "2023-01-13")
    assert len(ids) == 10 and g.shape == (10, 192)
    for i, a in enumerate(ids):
        ss = sessions[a]
        assert [x.start for x in ss] == [0, 96]                            # local midnight each day
        # deadlines come from the table and differ by data center: 04:30 to 10:30 the next day (+18 to +42 steps);
        # the first version assumed 04:30 for all (never run until the table reached Maritime, Nov 19)
        assert all(x.start + 96 + 18 <= x.deadline <= x.start + 96 + 42 for x in ss)
        assert DC.check(g[i], g[i], ss, 0.25) == []                        # the recorded schedule is valid
        for x in ss:
            assert x.energy_kwh == pytest.approx(g[i, x.start:x.start + 96].sum() * 0.25, rel=1e-6)


@pytest.mark.skipif(not (DATA / "dc_sessions_2023.parquet").exists(), reason="needs ../data with the session table")
def test_deadlines_are_the_tables_departure_times():
    import pandas as pd

    from sim.data import load_dc
    ids, g, sessions = load_dc(DATA, "2023-01-12", "2023-01-13")
    t = pd.read_parquet(DATA / "dc_sessions_2023.parquet")
    t0 = pd.Timestamp("2023-01-12 06:00", tz="UTC")
    for a in ids:
        rows = t[(t.entity_id == a) & (t.arrival_time >= t0) & (t.arrival_time < t0 + pd.Timedelta(days=2))]
        want = sorted(int((r - t0) / pd.Timedelta("15min")) for r in rows.departure_time)
        assert [x.deadline for x in sessions[a]] == want


@pytest.mark.skipif(not (DATA / "dc_sessions_2023.parquet").exists(), reason="needs ../data with the session table")
def test_site_room_holds_the_recorded_workload():
    """room = dc_capacity_kw - (load_kw - dc_flexible_kw), at least 0: the recorded workload fits in it at every
    step (recorded load never exceeds site capacity), so the recorded timing stays a valid schedule."""
    from sim.data import load_dc, load_dc_room
    ids, g, sessions = load_dc(DATA, "2023-01-12", "2023-01-13")
    ids2, room = load_dc_room(DATA, "2023-01-12", "2023-01-13")
    assert list(ids2) == list(ids) and room.shape == g.shape and (room >= 0).all()
    assert (g <= room + 1e-6).all()
    for i, a in enumerate(ids):
        assert DC.check(g[i], g[i], sessions[a], 0.25, room=room[i]) == []
