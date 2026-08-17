"""Fund daemon: the healthy-line 6h heartbeat temperance (v0.5.79).

The poll + any top-up run every cycle; only the steady OK health line is tempered to a
heartbeat so `clifctl logs` isn't a 15-min metronome when everything is in band."""

from __future__ import annotations

import logging
import types
from dataclasses import dataclass, field

import clif.cli as cli


@dataclass
class _FakeHealth:
    severity: str = "OK"
    below: list = field(default_factory=list)
    funder_crit: bool = False
    error: str | None = None


class _DummyRpc:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_LINE = "💰 funding ✓ all 8 songbird accounts in band · ap-funder 1239 SGB"


def _patch(monkeypatch, health, line=_LINE):
    monkeypatch.setattr(cli, "RpcClient", _DummyRpc)
    monkeypatch.setattr(cli, "read_health", lambda rpc, net: health)
    monkeypatch.setattr(cli, "render_health", lambda fh, active=False: line)


def _count(caplog, text):
    return sum(1 for r in caplog.records if text in r.getMessage())


def _s():
    return types.SimpleNamespace(rpc_url="http://node", network="songbird")


def test_ok_line_tempered_then_heartbeats(monkeypatch, caplog):
    _patch(monkeypatch, _FakeHealth(severity="OK"))
    clock = {"t": 1000.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])
    caplog.set_level(logging.INFO, logger="clif")
    hb: dict = {}
    cli._fund_pass(dry_run=False, s=_s(), ok_hb=hb)  # first → logs
    cli._fund_pass(dry_run=False, s=_s(), ok_hb=hb)  # same line, <6h → tempered
    assert _count(caplog, "in band") == 1
    clock["t"] += 6 * 3600 + 1  # heartbeat window elapsed
    cli._fund_pass(dry_run=False, s=_s(), ok_hb=hb)  # → logs again
    assert _count(caplog, "in band") == 2


def test_ok_line_logs_on_change(monkeypatch, caplog):
    health = _FakeHealth(severity="OK")
    _patch(monkeypatch, health)
    monkeypatch.setattr(cli.time, "monotonic", lambda: 500.0)  # frozen — no heartbeat
    caplog.set_level(logging.INFO, logger="clif")
    hb: dict = {}
    cli._fund_pass(dry_run=False, s=_s(), ok_hb=hb)
    # funder balance moved → the rendered line changes → must re-log despite <6h
    monkeypatch.setattr(cli, "render_health", lambda fh, active=False: _LINE.replace("1239", "1500"))
    cli._fund_pass(dry_run=False, s=_s(), ok_hb=hb)
    assert _count(caplog, "1239") == 1 and _count(caplog, "1500") == 1


def test_warn_never_tempered(monkeypatch, caplog):
    _patch(monkeypatch, _FakeHealth(severity="WARN"), line="⚠ ap-funder getting low")
    monkeypatch.setattr(cli.time, "monotonic", lambda: 500.0)
    caplog.set_level(logging.INFO, logger="clif")
    hb: dict = {}
    cli._fund_pass(dry_run=False, s=_s(), ok_hb=hb)
    cli._fund_pass(dry_run=False, s=_s(), ok_hb=hb)
    assert _count(caplog, "getting low") == 2  # every cycle — alarms are never tempered


def test_one_shot_always_logs(monkeypatch, caplog):
    _patch(monkeypatch, _FakeHealth(severity="OK"))
    monkeypatch.setattr(cli.time, "monotonic", lambda: 500.0)
    caplog.set_level(logging.INFO, logger="clif")
    cli._fund_pass(dry_run=False, s=_s())  # ok_hb=None (fund once) → always logs
    cli._fund_pass(dry_run=False, s=_s())
    assert _count(caplog, "in band") == 2


# ---- startup version banner (every daemon logs its clif version on start) --------


def test_daemon_start_banner_logs_version_and_network(caplog):
    import logging
    from clif import __version__

    caplog.set_level(logging.INFO, logger="clif")
    cli._log_daemon_start("observe", "songbird")
    msg = caplog.records[-1].getMessage()
    assert f"clif v{__version__}" in msg and "observe daemon" in msg and "network=songbird" in msg
