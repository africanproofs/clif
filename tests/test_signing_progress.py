"""Reward-signing progress: topic0 anchor, log decode, weight aggregation, CLI.

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
from clif.rpc import REWARDS_SIGNED_TOPIC0, RewardSignedLog, RpcClient, RpcError
from clif.signing_progress import _scan_from_block, compute_signing_progress

# Independently-pinned anchor: full keccak of the canonical RewardsSigned signature.
# A mismatch = a vendored-keccak regression OR a struct-canonicalization slip (the
# (uint256,uint256)[] member) — either silently breaks the eth_getLogs topic filter.
EXPECTED_TOPIC0 = "0x81b5504045130d3b82498ff414ad58271e85bbde420cc85aa66d91eff9af30fb"

SGB = _NETWORKS["songbird"]
SPA_A = "0x" + "a1" * 20
SPA_B = "0x" + "b2" * 20
VOTER_A = "0x" + "c3" * 20
VOTER_B = "0x" + "d4" * 20


# ---- topic0 anchor ----


def test_topic0_matches_pinned_anchor():
    assert REWARDS_SIGNED_TOPIC0 == EXPECTED_TOPIC0


def test_topic0_is_full_keccak_not_selector():
    sig = b"RewardsSigned(uint24,address,address,bytes32,(uint256,uint256)[],uint64,bool)"
    assert REWARDS_SIGNED_TOPIC0 == "0x" + keccak256(sig).hex()
    assert len(REWARDS_SIGNED_TOPIC0) == 66  # 0x + 64 hex = full 32 bytes (not a 4-byte selector)


# ---- RpcClient.reward_signed_logs / block_number (httpx MockTransport) ----


def _rpc_client(handler) -> RpcClient:
    rpc = RpcClient("http://node:8545")
    rpc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return rpc


def _topic_addr(addr: str) -> str:
    return "0x" + "00" * 12 + addr[2:]


RHASH = b"\xab" * 32
RHASH_HEX = "0x" + RHASH.hex()


def _log_entry(spa: str, voter: str, *, threshold: bool, ts: int, block: int) -> dict:
    data = abi_encode(
        ["bytes32", "(uint256,uint256)[]", "uint64", "bool"],
        [RHASH, [(1, 5)], ts, threshold],
    )
    return {
        "address": SGB.flare_systems_manager,
        "topics": [
            REWARDS_SIGNED_TOPIC0,
            "0x" + (404).to_bytes(32, "big").hex(),
            _topic_addr(spa),
            _topic_addr(voter),
        ],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block),
    }


def test_reward_signed_logs_decode():
    captured: list[dict] = []

    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        captured.append(body)
        result = [
            _log_entry(SPA_A, VOTER_A, threshold=False, ts=1_700_000_000, block=100),
            _log_entry(SPA_B, VOTER_B, threshold=True, ts=1_700_000_500, block=110),
        ]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    with _rpc_client(h) as rpc:
        logs = rpc.reward_signed_logs(SGB.flare_systems_manager, 404, 0, 200)

    assert captured[0]["method"] == "eth_getLogs"
    flt = captured[0]["params"][0]
    assert flt["address"] == SGB.flare_systems_manager
    assert flt["topics"] == [REWARDS_SIGNED_TOPIC0, "0x" + (404).to_bytes(32, "big").hex()]
    assert flt["fromBlock"] == "0x0" and flt["toBlock"] == hex(200)

    assert logs == [
        RewardSignedLog(SPA_A.lower(), VOTER_A.lower(), RHASH_HEX, False, 1_700_000_000, 100),
        RewardSignedLog(SPA_B.lower(), VOTER_B.lower(), RHASH_HEX, True, 1_700_000_500, 110),
    ]


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
        self._signers = signers  # list[RewardSignedLog]
        self._weights = weights  # {spa(lower): weight}
        self._latest = latest
        self._ppm = ppm
        self._total = total

    def block_number(self):
        return self._latest

    def reward_signed_logs(self, _fsm, _epoch, _lo, _hi):
        # Return the full set on every chunk → also exercises dedup-by-spa.
        return list(self._signers)

    def weights_sums(self, _vr, _epoch):
        return (10**20, self._total, self._total)

    def signing_policy_threshold_ppm(self, _fsm):
        return self._ppm

    def voter_normalised_weight(self, _vr, _epoch, spa):
        return ("0x" + "ee" * 20, self._weights[spa.lower()])


def _signer(spa, voter, threshold, block=100, rewards_hash=RHASH_HEX):
    return RewardSignedLog(spa.lower(), voter.lower(), rewards_hash, threshold, 1_700_000_000, block)


def test_compute_below_threshold_not_finalized():
    rpc = FakeRpc(
        signers=[_signer(SPA_A, VOTER_A, False)],
        weights={SPA_A.lower(): 697},  # 697 / 65506 ≈ 1.06%
    )
    sp = compute_signing_progress(rpc, SGB, 404, SPA_A, now=1_700_010_000, epoch_end_ts=1_700_000_000)
    assert sp.signed_weight == 697
    assert sp.total_weight == 65506
    assert round(sp.signed_pct, 2) == 1.06
    assert sp.threshold_pct == 50.0
    assert sp.finalized is False
    assert sp.our_signed is True
    assert sp.signer_count == 1


def test_compute_threshold_reached_event_finalizes():
    rpc = FakeRpc(
        signers=[_signer(SPA_A, VOTER_A, False), _signer(SPA_B, VOTER_B, True)],
        weights={SPA_A.lower(): 697, SPA_B.lower(): 40_000},
    )
    sp = compute_signing_progress(rpc, SGB, 404, "0x" + "ff" * 20, now=1_700_010_000, epoch_end_ts=1_700_000_000)
    # 40697/65506 ≈ 62.1% AND an event carried thresholdReached=True
    assert sp.finalized is True
    assert sp.signer_count == 2
    assert sp.our_signed is False  # our spa not among signers
    # signers sorted by weight desc
    assert sp.signers[0].weight == 40_000


def test_compute_finalized_by_accumulated_weight():
    """No thresholdReached flag yet, but summed weight already exceeds 50%."""
    rpc = FakeRpc(
        signers=[_signer(SPA_A, VOTER_A, False)],
        weights={SPA_A.lower(): 33_000},  # > ceil(65506*0.5)=32753
    )
    sp = compute_signing_progress(rpc, SGB, 404, None, now=1_700_010_000, epoch_end_ts=1_700_000_000)
    assert sp.finalized is True
    assert sp.our_signed is False  # our_spa None


def test_compute_dedups_repeated_signers():
    rpc = FakeRpc(
        signers=[_signer(SPA_A, VOTER_A, False), _signer(SPA_A, VOTER_A, False)],
        weights={SPA_A.lower(): 697},
    )
    sp = compute_signing_progress(rpc, SGB, 404, SPA_A, now=1_700_010_000, epoch_end_ts=1_700_000_000)
    assert sp.signer_count == 1
    assert sp.signed_weight == 697  # counted once


def test_compute_groups_by_rewards_hash_reports_leading():
    """Voters split across two candidate hashes → report the LEADING hash only."""
    hash_win = "0x" + "11" * 32
    hash_lose = "0x" + "22" * 32
    rpc = FakeRpc(
        signers=[
            _signer(SPA_A, VOTER_A, False, rewards_hash=hash_win),  # 40000
            _signer(SPA_B, VOTER_B, False, rewards_hash=hash_lose),  # 697
        ],
        weights={SPA_A.lower(): 40_000, SPA_B.lower(): 697},
    )
    sp = compute_signing_progress(rpc, SGB, 404, SPA_B, now=1_700_010_000, epoch_end_ts=1_700_000_000)
    assert sp.rewards_hash == hash_win
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

    def reward_signed_logs(self, _fsm, _epoch, lo, hi):
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
    # ~1800s since epoch end → ~2000-block window → ~67 chunks of 30 < budget.
    sp = compute_signing_progress(rpc, SGB, 404, SPA_A, now=1_700_001_800, epoch_end_ts=1_700_000_000)
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
    # Epoch ended ~16h ago → window capped at 30000; at 30/req that's 1000 reqs > budget.
    sp = compute_signing_progress(rpc, SGB, 404, SPA_A, now=1_700_057_600, epoch_end_ts=1_700_000_000)
    assert sp.complete is False
    assert sp.scanned_from_block > latest - 30_000  # stopped before the window start
    assert sp.our_signed is True  # recent signer (near latest) still captured


def test_compute_raises_without_voter_registry():
    with pytest.raises(ValueError, match="VoterRegistry"):
        compute_signing_progress(
            FakeRpc(signers=[], weights={}), _NETWORKS["coston2"], 404, None,
            now=1.0, epoch_end_ts=0.0,
        )


# ---- block-window math (deterministic) ----


def test_scan_from_block_covers_window_with_overscan():
    # 2 hours since epoch end, 1.8s blocks → ~4000 blocks; over-scan(1.5)+margin → ~6500.
    latest = 1_000_000
    frm = _scan_from_block(SGB, latest, now=7_200.0, epoch_end_ts=0.0)
    # est = int(7200/1.8*1.5)+500 = 6000+500 = 6500
    assert frm == latest - 6500


def test_scan_from_block_capped():
    # epoch ended very long ago → window capped at _MAX_WINDOW_BLOCKS (30k).
    latest = 1_000_000
    frm = _scan_from_block(SGB, latest, now=10**9, epoch_end_ts=0.0)
    assert frm == latest - 30_000


def test_scan_from_block_floor_zero():
    frm = _scan_from_block(SGB, latest=100, now=10**9, epoch_end_ts=0.0)
    assert frm == 0


# ---- CLI: epoch signing-progress ----


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

    def reward_signed_logs(self, _fsm, _epoch, _lo, _hi):
        return [_signer(SPA_A, VOTER_A, False)]

    def weights_sums(self, _vr, _epoch):
        return (10**20, 65506, 65506)

    def signing_policy_threshold_ppm(self, _fsm):
        return 500_000

    def voter_normalised_weight(self, _vr, _epoch, _spa):
        return ("0x" + "ee" * 20, 697)


def test_cli_signing_progress_json(monkeypatch):
    from typer.testing import CliRunner
    from clif.cli import app
    from clif.config import Settings

    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(_env_file=None, network="songbird", signing_policy_address=SPA_A),
    )
    monkeypatch.setattr("clif.cli.RpcClient", _CliFakeRpc)

    result = CliRunner().invoke(app, ["epoch", "signing-progress", "--json"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["network"] == "songbird"
    assert parsed["epoch"] == 404  # default = current(405) - 1
    assert parsed["signed_pct"] == 1.06
    assert parsed["threshold_pct"] == 50.0
    assert parsed["finalized"] is False
    assert parsed["our_signed"] is True
    assert parsed["signer_count"] == 1
    assert parsed["complete"] is True
    assert "[bold" not in result.output


def test_cli_signing_progress_explicit_epoch_human(monkeypatch):
    from typer.testing import CliRunner
    from clif.cli import app
    from clif.config import Settings

    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(_env_file=None, network="songbird", signing_policy_address=SPA_A),
    )
    monkeypatch.setattr("clif.cli.RpcClient", _CliFakeRpc)

    result = CliRunner().invoke(app, ["epoch", "signing-progress", "--epoch", "404"])
    assert result.exit_code == 0, result.output
    assert "epoch 404 reward-signing" in result.output
    assert "signed" in result.output  # our vote present


def test_cli_signing_progress_coston2_exits_2(monkeypatch):
    """No VoterRegistry configured for coston2 → exit 2 (misconfig)."""
    from typer.testing import CliRunner
    from clif.cli import app
    from clif.config import Settings

    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(_env_file=None, network="coston2"),
    )
    result = CliRunner().invoke(app, ["epoch", "signing-progress", "--network", "coston2"])
    assert result.exit_code == 2


def test_cli_signing_progress_rpc_error_exits_1(monkeypatch):
    from typer.testing import CliRunner
    from clif.cli import app
    from clif.config import Settings

    class _FailRpc(_CliFakeRpc):
        def reward_epoch_timing(self, _fsm):
            raise RpcError("node down")

    monkeypatch.setattr(
        "clif.cli.load_settings",
        lambda: Settings(_env_file=None, network="songbird", signing_policy_address=SPA_A),
    )
    monkeypatch.setattr("clif.cli.RpcClient", _FailRpc)
    result = CliRunner().invoke(app, ["epoch", "signing-progress"])
    assert result.exit_code == 1
