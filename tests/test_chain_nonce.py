"""Tests for clif.rpc.get_transaction_count and clif chain nonce command."""

import json

import httpx
import pytest

from clif.rpc import RpcClient, RpcError


# ---- RpcClient.get_transaction_count ----


def _rpc_client(handler) -> RpcClient:
    rpc = RpcClient("http://node:8545")
    rpc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return rpc


def test_get_transaction_count_latest_hex_to_int():
    """eth_getTransactionCount with block_tag='latest' returns hex→int."""
    captured: list[dict] = []

    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        captured.append(body)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x1a"})

    with _rpc_client(h) as rpc:
        result = rpc.get_transaction_count("0x" + "ab" * 20, "latest")

    assert result == 26  # 0x1a == 26
    assert captured[0]["method"] == "eth_getTransactionCount"
    assert captured[0]["params"] == ["0x" + "ab" * 20, "latest"]


def test_get_transaction_count_pending():
    """eth_getTransactionCount with block_tag='pending' converts hex→int."""
    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x5"})

    with _rpc_client(h) as rpc:
        result = rpc.get_transaction_count("0x" + "ab" * 20, "pending")

    assert result == 5


def test_get_transaction_count_rpc_error_raises():
    """Transport failure raises RpcError (same as other read methods)."""
    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": "bad"}},
        )

    with _rpc_client(h) as rpc:
        with pytest.raises(RpcError):
            rpc.get_transaction_count("0x" + "ab" * 20)


# ---- clif chain nonce CLI command ----


def _make_rpc_class(latest_val: int, pending_val: int):
    """Return a RpcClient replacement whose get_transaction_count returns the supplied values."""

    class _FakeRpc:
        def __init__(self, *_a, **_kw):
            self._calls: list[tuple[str, str]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_transaction_count(self, address: str, block_tag: str = "latest") -> int:
            self._calls.append((address, block_tag))
            return latest_val if block_tag == "latest" else pending_val

    return _FakeRpc


def test_chain_nonce_json_output(monkeypatch):
    """chain nonce --json emits parseable JSON with correct keys and coston2 chain_id=114."""
    from typer.testing import CliRunner
    from clif.cli import app
    from clif.config import Settings

    addr = "0x" + "ab" * 20

    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(_env_file=None, network="coston2"),
    )
    monkeypatch.setattr("clif.cli.RpcClient", _make_rpc_class(10, 11))

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["chain", "nonce", "--network", "coston2", "--address", addr, "--json"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["network"] == "coston2"
    assert parsed["chain_id"] == 114
    assert parsed["address"] == addr
    assert parsed["latest"] == 10
    assert parsed["pending"] == 11
    # No rich markup in the JSON output
    assert "[bold" not in result.output


def test_chain_nonce_network_defaults_from_env(monkeypatch):
    """chain nonce WITHOUT --network resolves the network from settings (NETWORK env).

    This is the regression this change exists to enable: the `clif` host wrapper
    strips a leading `--network <N>` (env-selector) and runs `clif chain nonce
    --address 0x.. --json` with no command-level --network. The command must
    succeed and report the env-resolved network (here songbird), not error on a
    missing required option.
    """
    from typer.testing import CliRunner
    from clif.cli import app
    from clif.config import Settings

    addr = "0x" + "ef" * 20

    # Settings loaded as if NETWORK=songbird were in the env / selected .env.
    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(_env_file=None, network="songbird"),
    )
    monkeypatch.setattr("clif.cli.RpcClient", _make_rpc_class(3, 4))

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["chain", "nonce", "--address", addr, "--json"],  # NO --network
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["network"] == "songbird"  # defaulted from settings, not a flag
    assert parsed["chain_id"] == 19  # songbird chain_id
    assert parsed["address"] == addr
    assert parsed["latest"] == 3
    assert parsed["pending"] == 4
    assert "[bold" not in result.output


def test_chain_nonce_human_output(monkeypatch):
    """chain nonce without --json prints human-readable line."""
    from typer.testing import CliRunner
    from clif.cli import app
    from clif.config import Settings

    addr = "0x" + "cd" * 20

    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(_env_file=None, network="flare"),
    )
    monkeypatch.setattr("clif.cli.RpcClient", _make_rpc_class(7, 8))

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["chain", "nonce", "--network", "flare", "--address", addr],
    )
    assert result.exit_code == 0, result.output
    assert "latest=7" in result.output
    assert "pending=8" in result.output


def test_chain_nonce_rpc_error_exits_1(monkeypatch):
    """RPC failure from get_transaction_count exits 1."""
    from typer.testing import CliRunner
    from clif.cli import app
    from clif.config import Settings

    class _FailRpc:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_transaction_count(self, *_a, **_kw):
            raise RpcError("node down")

    addr = "0x" + "aa" * 20

    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(_env_file=None, network="coston2"),
    )
    monkeypatch.setattr("clif.cli.RpcClient", _FailRpc)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["chain", "nonce", "--network", "coston2", "--address", addr],
    )
    assert result.exit_code == 1


def test_chain_nonce_bad_address_exits_2(monkeypatch):
    """--address without 0x prefix exits 2 immediately."""
    from typer.testing import CliRunner
    from clif.cli import app
    from clif.config import Settings

    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(_env_file=None, network="coston2"),
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["chain", "nonce", "--network", "coston2", "--address", "notahexaddress"],
    )
    assert result.exit_code == 2


# ---- fsp help string regression ----


def test_fsp_help_reflects_clif_broadcasts():
    """fsp --help must say 'clif broadcasts' and must NOT say 'AND broadcasts'."""
    from typer.testing import CliRunner
    from clif.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["fsp", "--help"])
    assert "clif broadcasts" in result.output
    assert "AND broadcasts" not in result.output


# ---- keyless regression for chain nonce ----


def test_chain_nonce_exits_2_with_private_key_in_env(monkeypatch):
    """chain nonce inherits _settings() keyless check: exits 2 if PRIVATE_KEY in env."""
    from typer.testing import CliRunner
    from clif.cli import app

    monkeypatch.setenv("SOME_PRIVATE_KEY", "0xdeadbeef")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["chain", "nonce", "--network", "coston2", "--address", "0x" + "aa" * 20],
    )
    assert result.exit_code == 2
