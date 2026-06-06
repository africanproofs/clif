"""Merkle tree: byte-exact against real Flare epoch 228 (offline fixture).

Fixture: tests/fixtures/epoch228-reward-distribution-data.json
  Source: https://raw.githubusercontent.com/flare-foundation/fsp-rewards/main/flare/228/reward-distribution-data.json
  merkleRoot: 0x1f68e0d9e92745c7f636e1917cfb902c51433fb766969935c68988b9b72ea601
  rewardEpochId: 228, rewardClaims: 119

Tests run entirely offline — no live fetch, no live fwd, no private key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clif.merkle import (
    build_reward_merkle_root,
    compute_leaf,
    node,
    verify_proof,
)
from clif.models import RewardDistributionData

_FIXTURE = Path(__file__).parent / "fixtures" / "epoch228-reward-distribution-data.json"

EXPECTED_ROOT = "0x1f68e0d9e92745c7f636e1917cfb902c51433fb766969935c68988b9b72ea601"
EXPECTED_EPOCH_ID = 228


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def epoch228_raw() -> dict:
    return json.loads(_FIXTURE.read_text())


@pytest.fixture(scope="module")
def epoch228(epoch228_raw: dict) -> RewardDistributionData:
    return RewardDistributionData.model_validate(epoch228_raw)


# ---------------------------------------------------------------------------
# (a) build_reward_merkle_root matches the published merkleRoot
# ---------------------------------------------------------------------------


def test_build_root_matches_published(epoch228: RewardDistributionData) -> None:
    """Recomputed root must equal the fixture's published merkleRoot byte-exactly."""
    recomputed = build_reward_merkle_root(c.body for c in epoch228.reward_claims)
    assert (
        recomputed.lower() == EXPECTED_ROOT.lower()
    ), f"recomputed={recomputed} published={EXPECTED_ROOT}"


def test_build_root_with_all_119_claims(epoch228: RewardDistributionData) -> None:
    """Fixture has 119 claims; confirms full-tree traversal."""
    assert len(epoch228.reward_claims) == 119
    root = build_reward_merkle_root(c.body for c in epoch228.reward_claims)
    assert root.lower() == EXPECTED_ROOT.lower()


# ---------------------------------------------------------------------------
# (b) verify_proof — sampled claims must pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("idx", [0, 1, 50, 118])
def test_verify_proof_sampled_claims(epoch228: RewardDistributionData, idx: int) -> None:
    """verify_proof returns True for real claims at various indices."""
    claim = epoch228.reward_claims[idx]
    leaf = compute_leaf(
        claim.body.reward_epoch_id,
        claim.body.beneficiary,
        int(claim.body.amount),
        claim.body.claim_type,
    )
    assert verify_proof(leaf, claim.merkle_proof, EXPECTED_ROOT) is True


# ---------------------------------------------------------------------------
# (c) tampered proof / amount / root must fail
# ---------------------------------------------------------------------------


def test_verify_proof_tampered_proof_fails(epoch228: RewardDistributionData) -> None:
    """A corrupted proof element causes verify_proof to return False."""
    claim = epoch228.reward_claims[0]
    leaf = compute_leaf(
        claim.body.reward_epoch_id,
        claim.body.beneficiary,
        int(claim.body.amount),
        claim.body.claim_type,
    )
    bad_proof = list(claim.merkle_proof)
    bad_proof[0] = "0x" + "ab" * 32  # corrupt first sibling
    assert verify_proof(leaf, bad_proof, EXPECTED_ROOT) is False


def test_verify_proof_tampered_amount_fails(epoch228: RewardDistributionData) -> None:
    """A modified claim amount produces a different leaf → proof fails."""
    claim = epoch228.reward_claims[0]
    bad_leaf = compute_leaf(
        claim.body.reward_epoch_id,
        claim.body.beneficiary,
        int(claim.body.amount) + 1,  # off by one
        claim.body.claim_type,
    )
    assert verify_proof(bad_leaf, claim.merkle_proof, EXPECTED_ROOT) is False


def test_verify_proof_tampered_root_fails(epoch228: RewardDistributionData) -> None:
    """A wrong root causes verify_proof to return False even for a valid proof."""
    claim = epoch228.reward_claims[0]
    leaf = compute_leaf(
        claim.body.reward_epoch_id,
        claim.body.beneficiary,
        int(claim.body.amount),
        claim.body.claim_type,
    )
    wrong_root = "0x" + "0" * 64
    assert verify_proof(leaf, claim.merkle_proof, wrong_root) is False


# ---------------------------------------------------------------------------
# Edge cases: n==0 and n==1
# ---------------------------------------------------------------------------


def test_empty_claim_set_root_is_zero() -> None:
    """An empty claim iterable returns the all-zero hash."""
    root = build_reward_merkle_root([])
    assert root == "0x" + "0" * 64


def test_single_claim_root_equals_leaf() -> None:
    """A single-claim tree: root == leaf (no internal node computation)."""

    class _FakeClaim:
        reward_epoch_id: int = 1
        beneficiary: str = "0x" + "aa" * 20
        amount: int = 1_000_000
        claim_type: int = 0

    leaf = compute_leaf(
        _FakeClaim.reward_epoch_id,
        _FakeClaim.beneficiary,
        _FakeClaim.amount,
        _FakeClaim.claim_type,
    )
    root = build_reward_merkle_root([_FakeClaim()])
    assert root == leaf


def test_single_claim_verify_empty_proof() -> None:
    """For a single-claim tree the proof is empty and verify_proof passes."""

    class _FakeClaim:
        reward_epoch_id: int = 1
        beneficiary: str = "0x" + "bb" * 20
        amount: int = 500
        claim_type: int = 1

    leaf = compute_leaf(
        _FakeClaim.reward_epoch_id,
        _FakeClaim.beneficiary,
        _FakeClaim.amount,
        _FakeClaim.claim_type,
    )
    root = build_reward_merkle_root([_FakeClaim()])
    assert verify_proof(leaf, [], root) is True


# ---------------------------------------------------------------------------
# compute_leaf determinism and case-insensitivity
# ---------------------------------------------------------------------------


def test_compute_leaf_is_deterministic(epoch228: RewardDistributionData) -> None:
    """compute_leaf returns the same result on repeated calls."""
    c = epoch228.reward_claims[0].body
    leaf_a = compute_leaf(c.reward_epoch_id, c.beneficiary, int(c.amount), c.claim_type)
    leaf_b = compute_leaf(c.reward_epoch_id, c.beneficiary, int(c.amount), c.claim_type)
    assert leaf_a == leaf_b


def test_compute_leaf_beneficiary_case_insensitive(epoch228: RewardDistributionData) -> None:
    """Beneficiary address normalised to lowercase before encoding."""
    c = epoch228.reward_claims[0].body
    leaf_lower = compute_leaf(c.reward_epoch_id, c.beneficiary.lower(), int(c.amount), c.claim_type)
    leaf_upper = compute_leaf(c.reward_epoch_id, c.beneficiary.upper(), int(c.amount), c.claim_type)
    assert leaf_lower == leaf_upper


# ---------------------------------------------------------------------------
# node commutativity
# ---------------------------------------------------------------------------


def test_node_is_commutative() -> None:
    """node(x, y) == node(y, x) because it sorts internally."""
    x = "0x" + "aa" * 32
    y = "0x" + "bb" * 32
    assert node(x, y) == node(y, x)


def test_node_same_inputs() -> None:
    """node(x, x) is well-defined (both sides equal after sort)."""
    x = "0x" + "cc" * 32
    result = node(x, x)
    assert result.startswith("0x")
    assert len(result) == 66


# ---------------------------------------------------------------------------
# Fixture sanity: all claims in the fixture verify against the published root
# ---------------------------------------------------------------------------


def test_all_claims_verify_against_published_root(epoch228: RewardDistributionData) -> None:
    """Every one of the 119 fixture claims has a valid proof."""
    failures = []
    for i, claim in enumerate(epoch228.reward_claims):
        leaf = compute_leaf(
            claim.body.reward_epoch_id,
            claim.body.beneficiary,
            int(claim.body.amount),
            claim.body.claim_type,
        )
        if not verify_proof(leaf, claim.merkle_proof, EXPECTED_ROOT):
            failures.append(i)
    assert failures == [], f"proof verification failed for claim indices: {failures}"
