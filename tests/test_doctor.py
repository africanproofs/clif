"""clif doctor (consumer self-check) + status --json — the coordinator seam (ADR-0001)."""

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from clif.cli import app
from clif.config import Settings


def _fake_fwd_class(master="ok", exc=None):
    class _F:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def health(self):
            if exc is not None:
                raise exc
            return SimpleNamespace(master=master, fwd="ok")

    return _F


def _patch_settings(monkeypatch, tmp_path, **kw):
    base = dict(_env_file=None, network="songbird", clif_state_dir=str(tmp_path))
    base.update(kw)
    monkeypatch.setattr("clif.cli.load_settings", lambda: Settings(**base))


def test_doctor_json_healthy_when_fwd_ready(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path, fwd_caller_token="TOK-SECRET")
    monkeypatch.setattr("clif.cli.FwdClient", _fake_fwd_class(master="ok"))

    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["consumer"] == "clif"
    assert payload["network"] == "songbird"
    assert payload["ok"] is True
    assert payload["keyless"] is True
    assert set(payload["compat"]) == {"fwd_contract_expected", "fwd_client", "clif"}
    assert payload["fwd"]["reachable"] is True and payload["fwd"]["master"] == "ok"
    assert [c["capability_id"] for c in payload["capabilities"]] == [
        "clif/songbird/claim",
        "clif/songbird/fsp-sign",
        "clif/songbird/fsp-submit",
    ]
    # claim has its caller token; FSP tokens unset -> reflects clif's imported view
    by_role = {c["role"]: c["configured"] for c in payload["capabilities"]}
    assert by_role == {"claim": True, "fsp-sign": False, "fsp-submit": False}
    # no daemon running in tmp state dir -> informational, not a failure
    assert payload["daemon"]["present"] is False
    assert "TOK-SECRET" not in result.output  # caller-token value never emitted


def test_doctor_fails_when_fwd_unreachable(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "clif.cli.FwdClient", _fake_fwd_class(exc=RuntimeError("connection refused"))
    )
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["fwd"]["reachable"] is False
    assert "connection refused" in payload["fwd"]["error"]


def test_doctor_fails_when_master_not_ok(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr("clif.cli.FwdClient", _fake_fwd_class(master="unavailable"))
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["ok"] is False


def test_epoch_status_json_no_daemon_state(monkeypatch, tmp_path):
    _patch_settings(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["epoch", "status", "--json"])
    assert result.exit_code == 3  # no daemon state
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["exit_code"] == 3
    assert payload["report"] is None
    assert "[bold" not in result.output  # clean JSON, no rich markup
