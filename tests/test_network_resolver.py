"""clif refuses to silently default the chain to flare (D20).

NETWORK is clif's only chain selector; an unset NETWORK with no --network used to
default to flare — a wrong-chain signing/claiming risk. The shared resolver (the
`preflight` command) and the `epoch run` daemon now hard-fail instead.
"""

from typer.testing import CliRunner

from clif.cli import app

# A well-formed (but arbitrary) address — enough to pass the format check and
# reach the network resolver, which fires before any chain call.
_VALID_ADDR = "0x" + "ab" * 20


def test_preflight_refuses_silent_flare_default(monkeypatch):
    monkeypatch.delenv("NETWORK", raising=False)
    result = CliRunner().invoke(app, ["preflight", "--identity", _VALID_ADDR])
    assert result.exit_code == 2, result.output
    assert "no network selected" in result.output
    assert "flare" in result.output  # the refusal names what it won't default to
