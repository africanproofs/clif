"""Calldata: signature derived from the real ABI, selector anchored, round-trips.

The synthetic proof here is for the ENCODER unit test only; canonical-prompt
constraint 3 (no hand-authored shapes) governs the deliverable-2 spec
artifact, not encoder coverage.
"""

from eth_abi import decode as abi_decode

from clif.calldata import (
    CLAIM_SELECTOR,
    CLAIM_SIGNATURE,
    EXPECTED_CLAIM_SELECTOR,
    _CLAIM_ARG_TYPES,
    build_claim_calldata,
)
from clif.models import RewardClaimBody, RewardClaimWithProof


def test_signature_reconstructed_from_abi():
    assert CLAIM_SIGNATURE == (
        "claim(address,address,uint24,bool,"
        "(bytes32[],(uint24,bytes20,uint120,uint8))[])"
    )


def test_selector_matches_verified_anchor():
    assert CLAIM_SELECTOR.hex() == EXPECTED_CLAIM_SELECTOR == "8e33aba5"


def test_build_claim_calldata_round_trips():
    owner = "0x" + "11" * 20
    recipient = "0x" + "22" * 20
    benef = "0x" + "33" * 20
    proof_hashes = ["0x" + "ab" * 32, "0x" + "cd" * 32]
    proofs = [
        RewardClaimWithProof(
            merkle_proof=proof_hashes,
            body=RewardClaimBody(
                reward_epoch_id=321,
                beneficiary=benef,
                amount=10**24,
                claim_type=1,
            ),
        )
    ]
    data = build_claim_calldata(owner, recipient, 321, True, proofs)

    assert data.startswith("0x" + CLAIM_SELECTOR.hex())
    payload = bytes.fromhex(data[10:])
    d_owner, d_recip, d_epoch, d_wrap, d_proofs = abi_decode(_CLAIM_ARG_TYPES, payload)

    assert d_owner.lower() == owner
    assert d_recip.lower() == recipient
    assert d_epoch == 321
    assert d_wrap is True
    (mproof, body) = d_proofs[0]
    assert [p.hex() for p in mproof] == ["ab" * 32, "cd" * 32]
    assert body[0] == 321
    assert body[1] == bytes.fromhex("33" * 20)  # bytes20 beneficiary
    assert body[2] == 10**24
    assert body[3] == 1
