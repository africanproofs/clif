"""Signing progress (uptime + rewards): topic0 anchors, log decode, aggregation, CLI.

The RPC-layer decode is exercised with httpx MockTransport (mirrors
test_chain_nonce.py); the aggregation + block-window math with a FakeRpc
(mirrors test_epoch_auto.py). The live cross-checks (real Songbird/Flare,
contract-getter agreement) are Core-#14 build verification, not unit tests.
"""

from __future__ import annotations

import json

import httpx
import pytest
from eth_abi import encode as abi_encode

from clif._keccak import keccak256
from clif.config import _NETWORKS
from clif.rpc import (
    REWARDS_SIGNED_TOPIC0,
    UPTIME_VOTE_SIGNED_TOPIC0,
    RpcClient,
    RpcError,
    SignedLog,
)
from clif.signing_progress import compute_signing_progress, refresh_signing_progress

# Independently-pinned anchors: full keccak of each canonical event signature.
# A mismatch = a vendored-keccak regression OR a struct-canonicalization slip (e.g.
# the rewards (uint256,uint256)[] member) — either silently breaks the topic filter.
EXPECTED_REWARDS_TOPIC0 = "0x81b5504045130d3b82498ff414ad58271e85bbde420cc85aa66d91eff9af30fb"
EXPECTED_UPTIME_TOPIC0 = "0x5506337d1266599f8b64675a1c8321701657ca2f2f70be0e0c58302b6c22e797"

SGB = _NETWORKS["songbird"]
SPA_A = "0x" + "a1" * 20
SPA_B = "0x" + "b2" * 20
VOTER_A = "0x" + "c3" * 20
VOTER_B = "0x" + "d4" * 20
MHASH = b"\xab" * 32
MHASH_HEX = "0x" + MHASH.hex()


# ---- topic0 anchors ----


def test_rewards_topic0_matches_pinned_anchor():
    assert REWARDS_SIGNED_TOPIC0 == EXPECTED_REWARDS_TOPIC0
    sig = b"RewardsSigned(uint24,address,address,bytes32,(uint256,uint256)[],uint64,bool)"
    assert REWARDS_SIGNED_TOPIC0 == "0x" + keccak256(sig).hex()


def test_uptime_topic0_matches_pinned_anchor():
    assert UPTIME_VOTE_SIGNED_TOPIC0 == EXPECTED_UPTIME_TOPIC0
    sig = b"UptimeVoteSigned(uint24,address,address,bytes32,uint64,bool)"
    assert UPTIME_VOTE_SIGNED_TOPIC0 == "0x" + keccak256(sig).hex()


def test_topic0s_are_full_keccak_not_selector():
    for t in (REWARDS_SIGNED_TOPIC0, UPTIME_VOTE_SIGNED_TOPIC0):
        assert len(t) == 66  # 0x + 64 hex = full 32 bytes (not a 4-byte selector)


# ---- RpcClient.signed_logs / block_number (httpx MockTransport) ----


def _rpc_client(handler) -> RpcClient:
    rpc = RpcClient("http://node:8545")
    rpc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return rpc


def _topic_addr(addr: str) -> str:
    return "0x" + "00" * 12 + addr[2:]


def _log_entry(spa: str, voter: str, *, threshold: bool, ts: int, block: int, kind="rewards") -> dict:
    if kind == "uptime":
        topic0 = UPTIME_VOTE_SIGNED_TOPIC0
        data = abi_encode(["bytes32", "uint64", "bool"], [MHASH, ts, threshold])
    else:
        topic0 = REWARDS_SIGNED_TOPIC0
        data = abi_encode(
            ["bytes32", "(uint256,uint256)[]", "uint64", "bool"], [MHASH, [(1, 5)], ts, threshold]
        )
    return {
        "address": SGB.flare_systems_manager,
        "topics": [
            topic0,
            "0x" + (404).to_bytes(32, "big").hex(),
            _topic_addr(spa),
            _topic_addr(voter),
        ],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block),
    }


def _decode_logs_test(kind: str, expected_topic0: str):
    captured: list[dict] = []

    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        captured.append(body)
        result = [
            _log_entry(SPA_A, VOTER_A, threshold=False, ts=1_700_000_000, block=100, kind=kind),
            _log_entry(SPA_B, VOTER_B, threshold=True, ts=1_700_000_500, block=110, kind=kind),
        ]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    with _rpc_client(h) as rpc:
        logs = rpc.signed_logs(SGB.flare_systems_manager, 404, 0, 200, kind=kind)

    assert captured[0]["method"] == "eth_getLogs"
    flt = captured[0]["params"][0]
    assert flt["topics"] == [expected_topic0, "0x" + (404).to_bytes(32, "big").hex()]
    assert flt["fromBlock"] == "0x0" and flt["toBlock"] == hex(200)
    assert logs == [
        SignedLog(SPA_A.lower(), VOTER_A.lower(), MHASH_HEX, False, 1_700_000_000, 100),
        SignedLog(SPA_B.lower(), VOTER_B.lower(), MHASH_HEX, True, 1_700_000_500, 110),
    ]


def test_signed_logs_decode_rewards():
    _decode_logs_test("rewards", REWARDS_SIGNED_TOPIC0)


def test_signed_logs_decode_uptime():
    # Uptime data is (bytes32, uint64, bool) — no claims array; threshold at idx 2.
    _decode_logs_test("uptime", UPTIME_VOTE_SIGNED_TOPIC0)


def test_block_number_hex_to_int():
    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x1a2b"})

    with _rpc_client(h) as rpc:
        assert rpc.block_number() == 0x1A2B


def test_weight_reads_decode_live_captured_raw():
    """getVoterWithNormalisedWeight + getWeightsSums against real Songbird-404 return data."""
    vw_raw = (
        "0x000000000000000000000000cf3a3e5797a960c67e0e4b23d4594246ffb9d935"
        "00000000000000000000000000000000000000000000000000000000000002b9"
    )
    ws_raw = (
        "0x00000000000000000000000000000000000000000000006b180cd8ca63af39fd"
        "000000000000000000000000000000000000000000000000000000000000ffe2"
        "000000000000000000000000000000000000000000000000000000000000ffe2"
    )

    from clif.calldata import selector

    vw_sel = "0x" + selector("getVoterWithNormalisedWeight(uint256,address)").hex()

    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        data = body["params"][0]["data"]
        result = vw_raw if data.startswith(vw_sel) else ws_raw
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    with _rpc_client(h) as rpc:
        voter, weight = rpc.voter_normalised_weight(SGB.voter_registry, 404, SPA_A)
        ws, nws, nwspk = rpc.weights_sums(SGB.voter_registry, 404)

    assert voter.lower() == "0xcf3a3e5797a960c67e0e4b23d4594246ffb9d935"
    assert weight == 697
    assert (nws, nwspk) == (65506, 65506)


# ---- compute_signing_progress (FakeRpc) ----


class FakeRpc:
    def __init__(self, *, signers, weights, latest=1_000_000, ppm=500_000, total=65_506):
        self._signers = signers  # list[SignedLog]
        self._weights = weights  # {spa(lower): weight}
        self._latest = latest
        self._ppm = ppm
        self._total = total

    def block_number(self):
        return self._latest

    def block_timestamp(self, _n):
        return 0  # < any epoch_end_ts → scan stops after the first chunk

    def signed_logs(self, _fsm, _epoch, _lo, _hi, *, kind="rewards"):
        # Return the full set on every chunk → also exercises dedup-by-spa.
        return list(self._signers)

    def weights_sums(self, _vr, _epoch):
        return (10**20, self._total, self._total)

    def signing_policy_threshold_ppm(self, _fsm):
        return self._ppm

    def voter_normalised_weight(self, _vr, _epoch, spa):
        return ("0x" + "ee" * 20, self._weights[spa.lower()])


def _signer(spa, voter, threshold, block=100, message_hash=MHASH_HEX):
    return SignedLog(spa.lower(), voter.lower(), message_hash, threshold, 1_700_000_000, block)


def test_compute_below_threshold_not_finalized():
    rpc = FakeRpc(
        signers=[_signer(SPA_A, VOTER_A, False)],
        weights={SPA_A.lower(): 697},  # 697 / 65506 ≈ 1.06%
    )
    sp = compute_signing_progress(rpc, SGB, 404, SPA_A, epoch_end_ts=1_700_000_000)
    assert sp.kind == "rewards"
    assert sp.signed_weight == 697
    assert sp.total_weight == 65506
    assert round(sp.signed_pct, 2) == 1.06
    assert sp.threshold_pct == 50.0
    assert sp.finalized is False
    assert sp.our_signed is True
    assert sp.signer_count == 1


def test_compute_uptime_kind():
    rpc = FakeRpc(
        signers=[_signer(SPA_A, VOTER_A, True)],
        weights={SPA_A.lower(): 40_000},  # > threshold AND thresholdReached flag
    )
    sp = compute_signing_progress(
        rpc, SGB, 404, SPA_A, epoch_end_ts=1_700_000_000, kind="uptime"
    )
    assert sp.kind == "uptime"
    assert sp.finalized is True
    assert sp.our_signed is True
    assert sp.message_hash == MHASH_HEX


def test_compute_threshold_reached_event_finalizes():
    rpc = FakeRpc(
        signers=[_signer(SPA_A, VOTER_A, False), _signer(SPA_B, VOTER_B, True)],
        weights={SPA_A.lower(): 697, SPA_B.lower(): 40_000},
    )
    sp = compute_signing_progress(rpc, SGB, 404, "0x" + "ff" * 20, epoch_end_ts=1_700_000_000)
    # 40697/65506 ≈ 62.1% AND an event carried thresholdReached=True
    assert sp.finalized is True
    assert sp.signer_count == 2
    assert sp.our_signed is False  # our spa not among signers
    assert sp.signers[0].weight == 40_000  # sorted by weight desc


def test_compute_finalized_by_accumulated_weight():
    """No thresholdReached flag yet, but summed weight already exceeds 50%."""
    rpc = FakeRpc(
        signers=[_signer(SPA_A, VOTER_A, False)],
        weights={SPA_A.lower(): 33_000},  # > ceil(65506*0.5)=32753
    )
    sp = compute_signing_progress(rpc, SGB, 404, None, epoch_end_ts=1_700_000_000)
    assert sp.finalized is True
    assert sp.our_signed is False  # our_spa None


def test_compute_dedups_repeated_signers():
    rpc = FakeRpc(
        signers=[_signer(SPA_A, VOTER_A, False), _signer(SPA_A, VOTER_A, False)],
        weights={SPA_A.lower(): 697},
    )
    sp = compute_signing_progress(rpc, SGB, 404, SPA_A, epoch_end_ts=1_700_000_000)
    assert sp.signer_count == 1
    assert sp.signed_weight == 697  # counted once


def test_compute_groups_by_message_hash_reports_leading():
    """Voters split across two candidate hashes → report the LEADING hash only."""
    hash_win = "0x" + "11" * 32
    hash_lose = "0x" + "22" * 32
    rpc = FakeRpc(
        signers=[
            _signer(SPA_A, VOTER_A, False, message_hash=hash_win),  # 40000
            _signer(SPA_B, VOTER_B, False, message_hash=hash_lose),  # 697
        ],
        weights={SPA_A.lower(): 40_000, SPA_B.lower(): 697},
    )
    sp = compute_signing_progress(rpc, SGB, 404, SPA_B, epoch_end_ts=1_700_000_000)
    assert sp.message_hash == hash_win
    assert sp.signed_weight == 40_000  # leading hash only — NOT 40697 (sum across hashes)
    assert sp.signer_count == 1
    assert sp.our_signed is False  # our spa signed the LOSING hash → not counted as signed


class FakeRpcCapped:
    """Simulates a range-capped node (the public RPC's 30-block getLogs cap)."""

    def __init__(self, *, signers, weights, latest, cap=30, total=65_506, ppm=500_000):
        self._signers = signers
        self._weights = weights
        self._latest = latest
        self._cap = cap
        self._total = total
        self._ppm = ppm

    def block_number(self):
        return self._latest

    def block_timestamp(self, n):
        return n  # block-number == timestamp scale for deterministic stop tests

    def signed_logs(self, _fsm, _epoch, lo, hi, *, kind="rewards"):
        if hi - lo + 1 > self._cap:
            raise RpcError(
                f"eth_getLogs rpc error: requested too many blocks from {lo} to {hi}, "
                f"maximum is set to {self._cap}"
            )
        return [s for s in self._signers if lo <= s.block_number <= hi]

    def weights_sums(self, _vr, _epoch):
        return (10**20, self._total, self._total)

    def signing_policy_threshold_ppm(self, _fsm):
        return self._ppm

    def voter_normalised_weight(self, _vr, _epoch, spa):
        return ("0x" + "ee" * 20, self._weights[spa.lower()])


def test_compute_adapts_to_range_cap_and_completes():
    """A 30-block-cap node: the scan auto-detects the cap, chunks to it, completes."""
    latest = 1_000_000
    rpc = FakeRpcCapped(
        signers=[_signer(SPA_A, VOTER_A, False, block=latest - 100)],
        weights={SPA_A.lower(): 697},
        latest=latest,
        cap=30,
    )
    # epoch_end at block latest-1000 → ~34 chunks of 30 (< budget) → completes.
    # (FakeRpcCapped.block_timestamp(n)=n, so epoch_end_ts is on the block scale.)
    sp = compute_signing_progress(rpc, SGB, 404, SPA_A, epoch_end_ts=latest - 1000)
    assert sp.complete is True
    assert sp.signer_count == 1
    assert sp.signed_weight == 697


def test_compute_budget_exhaustion_marks_incomplete():
    """A 30-block cap over a huge window exhausts the request budget → complete=False."""
    latest = 1_000_000
    rpc = FakeRpcCapped(
        signers=[_signer(SPA_A, VOTER_A, False, block=latest - 50)],  # within recent window
        weights={SPA_A.lower(): 697},
        latest=latest,
        cap=30,
    )
    # epoch_end at block 0 → never reached; at 30 blocks/req the budget (240) caps
    # the scan at ~7200 recent blocks → complete=False.
    sp = compute_signing_progress(rpc, SGB, 404, SPA_A, epoch_end_ts=0)
    assert sp.complete is False
    assert sp.scanned_from_block > latest - 30_000  # budget cut off well before block 0
    assert sp.our_signed is True  # recent signer (near latest) still captured


def test_compute_raises_without_voter_registry():
    with pytest.raises(ValueError, match="VoterRegistry"):
        compute_signing_progress(
            FakeRpc(signers=[], weights={}), _NETWORKS["coston2"], 404, None, epoch_end_ts=0.0
        )


def test_compute_stops_at_epoch_end_not_whole_chain():
    """The scan terminates once a chunk predates epoch-end (block-time independent)."""
    latest = 1_000_000
    rpc = FakeRpcCapped(
        signers=[_signer(SPA_A, VOTER_A, False, block=latest - 40)],
        weights={SPA_A.lower(): 697},
        latest=latest,
        cap=5000,  # uncapped-ish: one chunk covers the window
    )
    sp = compute_signing_progress(rpc, SGB, 404, SPA_A, epoch_end_ts=latest - 100)
    assert sp.complete is True
    assert sp.scanned_from_block == latest - 4999  # one 5000-chunk reached past epoch-end
    assert sp.our_signed is True


def test_httpx_request_logging_is_silenced():
    """Importing the CLI silences httpx/httpcore per-request INFO spam (daemon log flood)."""
    import logging

    import clif.cli  # noqa: F401  (import has the side effect of configuring logging)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# ---- refresh_signing_progress (cached/incremental) ----


class CountingRpc:
    """Counts RPC calls + supports an advancing chain head for incremental tests."""

    def __init__(self, signers, latest):
        import collections

        self.signers = signers  # list[SignedLog]
        self.latest = latest
        self.counts = collections.Counter()

    def block_number(self):
        self.counts["block_number"] += 1
        return self.latest

    def block_timestamp(self, n):
        self.counts["block_timestamp"] += 1
        return n  # block-number == timestamp scale

    def signed_logs(self, _fsm, _epoch, lo, hi, *, kind="rewards"):
        self.counts["signed_logs"] += 1
        return [s for s in self.signers if lo <= s.block_number <= hi]

    def weights_sums(self, _vr, _epoch):
        self.counts["weights_sums"] += 1
        return (10**20, 65506, 65506)

    def signing_policy_threshold_ppm(self, _fsm):
        self.counts["threshold_ppm"] += 1
        return 500_000

    def voter_normalised_weight(self, _vr, _epoch, spa):
        self.counts["weight"] += 1
        return ("0x" + "ee" * 20, 1000)


def test_refresh_caches_immutable_facts_and_scans_incrementally():
    cache: dict = {}
    rpc = CountingRpc(signers=[_signer(SPA_A, VOTER_A, False, block=3000)], latest=4000)

    # cycle 1 — full scan: fetches total + threshold + weight(A) once.
    sp1 = refresh_signing_progress(cache, rpc, SGB, 404, SPA_A, epoch_end_ts=0)
    assert sp1.signer_count == 1 and sp1.our_signed is True
    assert rpc.counts["weights_sums"] == 1
    assert rpc.counts["threshold_ppm"] == 1
    assert rpc.counts["weight"] == 1
    rpc.counts.clear()

    # cycle 2 — chain advanced 500 blocks, NO new signer: immutable facts NOT re-fetched,
    # only a forward getLogs over the new range + block_number. (~2 calls vs ~5.)
    rpc.latest = 4500
    sp2 = refresh_signing_progress(cache, rpc, SGB, 404, SPA_A, epoch_end_ts=0)
    assert sp2.signer_count == 1
    assert rpc.counts["weights_sums"] == 0  # cached
    assert rpc.counts["threshold_ppm"] == 0  # cached
    assert rpc.counts["weight"] == 0  # A already cached
    assert rpc.counts["signed_logs"] == 1  # one forward chunk over (4001..4500)
    rpc.counts.clear()

    # cycle 3 — a NEW signer appears above the high-water mark: incremental scan picks
    # it up, and ONLY its weight is looked up (A stays cached).
    rpc.signers.append(_signer(SPA_B, VOTER_B, False, block=4800))
    rpc.latest = 5000
    sp3 = refresh_signing_progress(cache, rpc, SGB, 404, SPA_A, epoch_end_ts=0)
    assert sp3.signer_count == 2
    assert rpc.counts["weight"] == 1  # only the new signer B
    assert rpc.counts["weights_sums"] == 0 and rpc.counts["threshold_ppm"] == 0


def test_refresh_matches_compute_on_first_pass():
    """A fresh refresh == a stateless compute over the same signers."""
    signers = [_signer(SPA_A, VOTER_A, True, block=3000)]
    a = compute_signing_progress(
        FakeRpc(signers=signers, weights={SPA_A.lower(): 1000}), SGB, 404, SPA_A, epoch_end_ts=1_700_000_000
    )
    rpc = CountingRpc(signers=signers, latest=4000)
    b = refresh_signing_progress({}, rpc, SGB, 404, SPA_A, epoch_end_ts=0)
    assert (a.signer_count, a.finalized, a.our_signed) == (b.signer_count, b.finalized, b.our_signed)


# ---- CLI: epoch signing-progress (both kinds + recipient) ----

RECIPIENT = "0x" + "7c" * 20


class _CliFakeRpc:
    def __init__(self, *_a, **_kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def reward_epoch_timing(self, _fsm):
        return (1658429955, 302400)

    def get_current_reward_epoch_id(self, _fsm):
        return 405

    def block_number(self):
        return 1_000_000

    def block_timestamp(self, _n):
        return 0  # < epoch_end → scan stops after the first chunk

    def signed_logs(self, _fsm, _epoch, _lo, _hi, *, kind="rewards"):
        return [_signer(SPA_A, VOTER_A, False)]

    def weights_sums(self, _vr, _epoch):
        return (10**20, 65506, 65506)

    def signing_policy_threshold_ppm(self, _fsm):
        return 500_000

    def voter_normalised_weight(self, _vr, _epoch, _spa):
        return ("0x" + "ee" * 20, 697)


def _cli_settings(**over):
    from clif.config import Settings

    base = dict(
        _env_file=None, network="songbird", signing_policy_address=SPA_A,
        claim_recipient_address=RECIPIENT,
    )
    base.update(over)
    return Settings(**base)


def test_cli_signing_progress_json(monkeypatch):
    from typer.testing import CliRunner
    from clif.cli import app

    monkeypatch.setattr("clif.cli.load_settings", lambda: _cli_settings())
    monkeypatch.setattr("clif.cli.RpcClient", _CliFakeRpc)

    result = CliRunner().invoke(app, ["epoch", "signing-progress", "--json"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["network"] == "songbird"
    assert parsed["epoch"] == 404  # default = current(405) - 1
    assert parsed["recipient"] == RECIPIENT
    assert parsed["our_voter"].lower() == SPA_A
    for kind in ("uptime", "rewards"):
        blk = parsed[kind]
        assert blk["kind"] == kind
        assert blk["signed_pct"] == 1.06
        assert blk["threshold_pct"] == 50.0
        assert blk["our_signed"] is True
        assert blk["signer_count"] == 1
        assert blk["complete"] is True
    assert "[bold" not in result.output


def test_cli_signing_progress_human_shows_both_and_recipient(monkeypatch):
    from typer.testing import CliRunner
    from clif.cli import app

    monkeypatch.setattr("clif.cli.load_settings", lambda: _cli_settings())
    monkeypatch.setattr("clif.cli.RpcClient", _CliFakeRpc)

    result = CliRunner().invoke(app, ["epoch", "signing-progress", "--epoch", "404"])
    assert result.exit_code == 0, result.output
    assert "uptime-signing" in result.output
    assert "reward-signing" in result.output
    assert RECIPIENT in result.output


def test_cli_signing_progress_coston2_exits_2(monkeypatch):
    """No VoterRegistry configured for coston2 → exit 2 (misconfig)."""
    from typer.testing import CliRunner
    from clif.cli import app

    monkeypatch.setattr("clif.cli.load_settings", lambda: _cli_settings(network="coston2"))
    result = CliRunner().invoke(app, ["epoch", "signing-progress", "--network", "coston2"])
    assert result.exit_code == 2


def test_cli_signing_progress_rpc_error_exits_1(monkeypatch):
    from typer.testing import CliRunner
    from clif.cli import app

    class _FailRpc(_CliFakeRpc):
        def reward_epoch_timing(self, _fsm):
            raise RpcError("node down")

    monkeypatch.setattr("clif.cli.load_settings", lambda: _cli_settings())
    monkeypatch.setattr("clif.cli.RpcClient", _FailRpc)
    result = CliRunner().invoke(app, ["epoch", "signing-progress"])
    assert result.exit_code == 1
