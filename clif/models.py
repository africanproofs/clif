"""Typed models: fsp-rewards distribution tuples + the fwd wire contract.

The fsp-rewards schema mirrors the upstream Zod schema in
`ftso-fee-claimer/src/interfaces.ts`. The fwd request/response models mirror
`fwd/src/fwd/api/sign.py` (verified against source this session);
`SignFspMessageResponse` ← `fwd/src/fwd/api/sign_fsp_message.py`; FSP message:
sign_fsp_message.py.
"""

from __future__ import annotations

import re
from enum import IntEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MERKLE_ROOT_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


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


class SignFspMessageResponse(BaseModel):
    """Response from fwd POST /v1/sign-fsp-message."""

    model_config = ConfigDict(extra="ignore")

    message_hash: str
    v: int
    r: str
    s: str
    signature: str


class RewardDistributionData(BaseModel):
    """reward-distribution-data.json (not tuples variant) — epoch id, merkle root + weight count."""

    model_config = ConfigDict(populate_by_name=True)

    reward_epoch_id: int = Field(alias="rewardEpochId")
    merkle_root: str = Field(alias="merkleRoot")
    no_of_weight_based_claims: int = Field(alias="noOfWeightBasedClaims")

    @field_validator("merkle_root")
    @classmethod
    def _validate_merkle_root(cls, v: str) -> str:
        if not _MERKLE_ROOT_RE.match(v):
            raise ValueError(
                f"merkleRoot must match ^0x[0-9a-fA-F]{{64}}$, got {v!r}"
            )
        return v

    @field_validator("no_of_weight_based_claims")
    @classmethod
    def _validate_no_of_weight_based_claims(cls, v: int) -> int:
        if v < 0:
            raise ValueError(
                f"noOfWeightBasedClaims must be an integer >= 0, got {v}"
            )
        return v
