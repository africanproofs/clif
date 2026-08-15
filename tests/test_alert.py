"""Push-alert logic: level derivation, debounce, re-page, resolve."""

from __future__ import annotations

from dataclasses import dataclass, field

from clif.alert import alert_level, decide, format_alert


@dataclass
class _Acct:
    name: str
    balance: float
    lower: float = 250.0
    below: bool = False
    nearing: bool = False


@dataclass
class _FH:
    severity: str = "OK"
    error: str | None = None
    funder_balance: float | None = 600.0
    below: list = field(default_factory=list)
    nearing: list = field(default_factory=list)
    funder_crit: bool = False
    funder_warn: bool = False


def _ready(**kw):
    base = dict(severity="OK", current_registered=True, gas_ok=True, entity_ok=True,
                votepower_ok=True, current_epoch=424, error=None)
    base.update(kw)
    return base


# ---- level derivation ------------------------------------------------------------


def test_level_ok_when_all_healthy():
    level, reasons = alert_level(_ready(), _FH())
    assert level == "OK" and reasons == []


def test_level_crit_on_exclusion():
    level, reasons = alert_level(_ready(severity="CRIT", current_registered=False), _FH())
    assert level == "CRIT"
    assert any("NOT REGISTERED" in r for r in reasons)


def test_level_crit_on_account_below_band():
    fh = _FH(severity="CRIT", below=[_Acct("Submit", 5.0)])
    level, reasons = alert_level(_ready(), fh)
    assert level == "CRIT" and any("Submit below band" in r for r in reasons)


def test_level_worst_of_both():
    # registration WARN + funding CRIT ⇒ CRIT
    fh = _FH(severity="CRIT", funder_crit=True, funder_balance=90.0)
    level, _ = alert_level(_ready(severity="WARN"), fh)
    assert level == "CRIT"


# ---- debounce + paging cadence ---------------------------------------------------


def test_transient_blip_does_not_page():
    st = {}
    # one bad read (confirm=2) → not yet confirmed → no page, level stays OK
    send, kind, st = decide(st, "CRIT", now=100.0, repeat_sec=3600, confirm=2)
    assert send is False and st["level"] == "OK"
    # recovers next read → still no page
    send, kind, st = decide(st, "OK", now=110.0, repeat_sec=3600, confirm=2)
    assert send is False and st["level"] == "OK"


def test_sustained_crit_pages_after_confirm():
    st = {}
    send, _, st = decide(st, "CRIT", now=100.0, repeat_sec=3600, confirm=2)
    assert send is False  # 1st CRIT — debounced
    send, kind, st = decide(st, "CRIT", now=110.0, repeat_sec=3600, confirm=2)
    assert send is True and kind == "ALERT" and st["level"] == "CRIT"


def test_repage_only_after_repeat_interval():
    st = {"level": "CRIT", "last_sent": 100.0, "pending": "CRIT", "pending_n": 5}
    send, _, st = decide(st, "CRIT", now=100.0 + 1800, repeat_sec=3600, confirm=2)
    assert send is False  # within repeat window
    send, kind, st = decide(st, "CRIT", now=100.0 + 3601, repeat_sec=3600, confirm=2)
    assert send is True and kind == "REMINDER"


def test_resolved_sent_on_recovery():
    st = {"level": "CRIT", "last_sent": 100.0, "pending": "CRIT", "pending_n": 5}
    send, _, st = decide(st, "OK", now=200.0, repeat_sec=3600, confirm=2)
    assert send is False  # 1st OK — debounced
    send, kind, st = decide(st, "OK", now=210.0, repeat_sec=3600, confirm=2)
    assert send is True and kind == "RESOLVED" and st["level"] == "OK"


def test_format_alert_shapes():
    msg = format_alert("flare", 424, "CRIT", ["NOT REGISTERED — earns ZERO"], "ALERT")
    assert "clif ALERT — flare RE424: CRIT" in msg and "• NOT REGISTERED" in msg
    ok = format_alert("flare", 424, "OK", [], "RESOLVED")
    assert "RESOLVED" in ok and "all clear" in ok
