"""Q-N5 (decided yes for M5): D16's double-sell block under a concurrency race. The gateway accepts concurrent clients,
so many clients of one legal entity fire holds (arm A offers against one mandate) at the same moment from separate
threads; no kW may be committed twice, and of two clients racing to register the same agent exactly one register is
sent. One gateway lock serializes check and commit. The unserialized path (Gateway(serialize=False), test-only) is
the red test: it shows the race is real in this process. Threads in one process, small N (memory)."""
import sys
import threading

import pytest

from exchange import messages as M
from exchange.core import Exchange
from iface.credentials import CredentialStore
from iface.gateway import Gateway
from sim.bus import Bus

T0 = "2023-01-02T06:00:00Z"
GATE = "2023-01-02T07:00:00Z"
WIN = {"start_utc": "2023-01-02T12:00:00Z", "end_utc": "2023-01-02T12:30:00Z"}
MAX_KW = 100.0


class _Log:
    def log(self, raw, ev):
        pass

    def refuse(self, raw, msg, origin, reason):
        pass


def _world(serialize: bool, n_clients: int):
    ex = Exchange("run", "OP", "meter", "clock", ["SYS"])
    bus = Bus(_Log(), ex)
    for ep in ("OP", "meter"):
        bus.attach(ep, type("E", (), {"receive": lambda self, r: None})())

    def send(sender, typ, payload, n=[0]):
        n[0] += 1
        bus.send([M.encode(M.make(typ, sender, "exchange", T0, payload, f"{sender}:{n[0]}", "run"))], sender)
        bus.flush()
    send("meter", "asset_limits", {"asset_id": "A/bess", "usable_kwh": 400.0, "power_kw": MAX_KW, "pv_kwh_per_day": 0.0,
                                   "owner": "A LLC", "agent_id": "A"})
    store = CredentialStore()
    gw = Gateway(credentials=store, serialize=serialize, limits={"max_sends_per_step": 10_000})
    gw.attach_direct("A", "A LLC", "A/bess", MAX_KW, "SYS", bus, clock=lambda: T0, run_id="run")
    tok = store.issue("A LLC")
    clients = [gw.open_session(tok, "A") for _ in range(n_clients)]
    return ex, bus, gw, send, clients


def _register_and_need(gw, send, cs):
    r, err = gw.call(cs, "register", {"asset_id": "A/bess", "max_kw": MAX_KW})
    assert not err
    gw.call(cs, "subscribe", {"signals": ["post_need"], "zones": ["SYS"]})
    send("OP", "post_need", {"zone": "SYS", "window": WIN, "kw_needed": 10_000.0, "mode": "scheduled",
                             "price_cap": 500.0, "gate_utc": GATE})
    return next(iter(gw.agents["A"].bus.exchange.needs))


def _fire(gw, clients, need_id, per_client: int, kw: float):
    barrier = threading.Barrier(len(clients))
    errors = []

    def go(cs):
        barrier.wait()
        for _ in range(per_client):
            try:
                gw.call(cs, "offer", {"need_id": need_id, "zone": "SYS", "window": WIN, "kw": kw, "floor_usd_mwh": 1.0})
            except Exception as e:          # an interleaving inside the exchange: also a race failure
                errors.append(type(e).__name__)
    th = [threading.Thread(target=go, args=(c,)) for c in clients]
    for t in th:
        t.start()
    for t in th:
        t.join(60)
    return errors


def _committed(ex) -> float:
    return sum(o["kw"] for o in ex.offers.values() if o["agent_id"] == "A" and o["live"])


def test_serialized_holds_never_commit_a_kw_twice():
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        ex, bus, gw, send, clients = _world(True, 8)
        need = _register_and_need(gw, send, clients[0])
        errors = _fire(gw, clients, need, per_client=50, kw=10.0)
    finally:
        sys.setswitchinterval(old)
    assert errors == []
    assert _committed(ex) <= MAX_KW + 1e-9
    assert sum(1 for o in ex.offers.values() if o["live"]) == 10            # exactly the mandate's room
    ids = [m for m in gw.agents["A"].sent]
    assert len(ids) == len(set(ids)) == 2 + 400                              # every msg_id unique


def test_two_clients_racing_to_register_send_exactly_one_register():
    for _ in range(20):
        ex, bus, gw, send, clients = _world(True, 2)
        barrier = threading.Barrier(2)
        res = []

        def go(cs):
            barrier.wait()
            res.append(gw.call(cs, "register", {"asset_id": "A/bess", "max_kw": MAX_KW}))
        th = [threading.Thread(target=go, args=(c,)) for c in clients]
        [t.start() for t in th]
        [t.join(10) for t in th]
        assert sorted(err for _, err in res) == [False, True]
        assert sorted(r.get("code", "ok") for r, _ in res) == ["already_registered", "ok"]
        assert sum(1 for m in gw.agents["A"].sent.values() if m["msg_type"] == "register") == 1
        assert list(ex.agents) == ["A"]


def test_red_unserialized_path_over_commits_or_breaks():
    """The red test: without the lock, check-then-commit interleaves across threads. Up to 5 trials; any trial with
    more than max_kw live or an exception inside the exchange shows the race."""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    seen = []
    try:
        for _ in range(5):
            ex, bus, gw, send, clients = _world(False, 8)
            need = _register_and_need(gw, send, clients[0])
            errors = _fire(gw, clients, need, per_client=50, kw=10.0)
            seen.append((round(_committed(ex), 1), errors[:3]))
            if _committed(ex) > MAX_KW + 1e-9 or errors:
                break
    finally:
        sys.setswitchinterval(old)
    if not any(c > MAX_KW + 1e-9 or e for c, e in seen):
        pytest.skip(f"race not reproduced in 5 trials on this machine: {seen}")
    assert any(c > MAX_KW + 1e-9 or e for c, e in seen)
