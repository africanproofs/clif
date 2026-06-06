"""fsp-rewards tuple parsing + the beneficiary/claimType find filter."""

from unittest.mock import MagicMock

import clif.discovery as discovery
from clif.config import Settings
from clif.models import RewardsData

# Proofs and merkleRoot computed from the claims below via clif.merkle so that
# reward_claim_for's proof-verification guard passes on these synthetic entries.
#   leaf(300, 0x11*20, 123456789, 1) = 0x58b20f...
#   leaf(300, 0x22*20,        999, 0) = 0x9ba901...
#   root = node(sorted leaves)       = 0xf35467...
_ROOT = "0xf35467d6fbb3a878cd696946268381e284547c1a2fab70ee80de461db99149e9"
_PROOF_B1 = ["0x9ba9019b5ab7ac433c56545d26cc56b733f28446d86b6cad96e0389ea9189511"]
_PROOF_B2 = ["0x58b20f22a2bc23bca3936c3b97eefd057ceb3694732c0fcd46f0537aa795487b"]

_FIXTURE = {
    "rewardEpochId": 300,
    "rewardClaims": [
        [_PROOF_B1, [300, "0x" + "11" * 20, "123456789", 1]],
        [_PROOF_B2, [300, "0x" + "22" * 20, "999", 0]],
    ],
    "noOfWeightBasedClaims": 0,
    "merkleRoot": _ROOT,
}


def test_rewards_data_parses_aliased_fields():
    d = RewardsData.model_validate(_FIXTURE)
    assert d.reward_epoch_id == 300
    assert len(d.reward_claims) == 2
    proof, (epoch, addr, amt, ctype) = d.reward_claims[0]
    assert proof == _PROOF_B1
    assert epoch == 300 and ctype == 1 and amt == "123456789"


def test_reward_claim_for_filters_by_beneficiary_and_type(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "get_reward_calculation_data",
        lambda _s, _e: RewardsData.model_validate(_FIXTURE),
    )
    s = Settings()
    # Mock rpc: rewards_hash returns the fixture's merkleRoot so the cross-check passes
    rpc = MagicMock()
    rpc.rewards_hash.return_value = _ROOT

    # case-insensitive beneficiary match, FEE (type 1)
    rc = discovery.reward_claim_for(rpc, s, 300, "0x" + "11" * 20, 1)
    assert rc is not None
    assert rc.body.amount == 123456789
    assert rc.body.claim_type == 1
    assert rc.merkle_proof == _PROOF_B1

    # DIRECT (type 0) for the other address
    rc0 = discovery.reward_claim_for(rpc, s, 300, "0X" + "22" * 20, 0)
    assert rc0 is not None and rc0.body.amount == 999

    # wrong claim type -> no match
    assert discovery.reward_claim_for(rpc, s, 300, "0x" + "11" * 20, 0) is None
