"""Typed models: fsp-rewards distribution tuples + the fwd wire contract.

The fsp-rewards schema mirrors the upstream Zod schema in
`ftso-fee-claimer/src/interfaces.ts`. The fwd request/response models mirror
`fwd/src/fwd/api/sign.py` (verified against source this session).
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, ConfigDict, Field


class ClaimType(IntEnum):
    DIRECT = 0
    FEE = 1
    WNAT = 2
    MIRROR = 3
    CCHAIN = 4


# Raw fsp-rewards tuples, exactly as published:
#   rewardClaims: [ [ merkleProof[], [epochId, beneficiary, amountStr, claimType] ], ... ]
RawClaimData = tuple[int, str, str, int]
RawRewardClaim = tuple[list[str], RawClaimData]


class RewardsData(BaseModel):
    """One reward epoch's distribution file (GitHub/GitLab fsp-rewards)."""

    model_config = ConfigDict(populate_by_name=True)

    reward_epoch_id: int = Field(alias="rewardEpochId")
    reward_claims: list[RawRewardClaim] = Field(alias="rewardClaims")
    no_of_weight_based_claims: int = Field(alias="noOfWeightBasedClaims", default=0)
    merkle_root: str = Field(alias="merkleRoot")


class RewardClaimBody(BaseModel):
    """The inner Solidity struct `(uint24, bytes20, uint120, uint8)`."""

    reward_epoch_id: int
    beneficiary: str  # 0x + 40 hex (encoded as bytes20 in calldata)
    amount: int
    claim_type: int


class RewardClaimWithProof(BaseModel):
    """`(bytes32[] merkleProof, RewardClaim body)` — one `_proofs[]` element."""

    merkle_proof: list[str]
    body: RewardClaimBody


# ---- fwd wire contract (verified: fwd/src/fwd/api/sign.py:38-161) ----


class SignAndSendRequest(BaseModel):
    wallet: str
    chain: int
    to: str
    value_wei: str = "0"
    data: str = "0x"
    gas: int | None = None


class SignAndSendResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tx_id: str
    hash: str
    nonce: int


class FwdError(BaseModel):
    model_config = ConfigDict(extra="ignore")

    error: str
    message: str


class TxStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    hashes: list[dict] = Field(default_factory=list)
    confirmed_at: str | None = None


class Health(BaseModel):
    model_config = ConfigDict(extra="allow")

    master: str | None = None
    rpc: object | None = None
    fwd: str | None = None
