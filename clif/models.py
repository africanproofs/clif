"""Typed models: fsp-rewards distribution tuples + fwd wire contract.

The fsp-rewards schema mirrors the upstream Zod schema in
`ftso-fee-claimer/src/interfaces.ts`.

The fwd request/response models (SignTransaction*, BroadcastResult*, Receipt*,
SignFspMessageResponse, TxStatus, Health) are now the single source of truth in
the shared `fwd_client` package (gitlab.com/proofs.africa/fwd-client v0.1.0).
They are re-exported here so existing callers (`from clif.models import …`)
continue to work with no change.
"""

from __future__ import annotations

import re
from enum import IntEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Re-export fwd wire contract models from the shared library.
from fwd_client import (  # noqa: F401
    BroadcastResultResponse,
    Health,
    ReceiptResponse,
    SignFspMessageResponse,
    SignTransactionResponse,
    TxStatus,
)
from fwd_client.models import (  # noqa: F401
    BroadcastResultRequest,
    ReceiptRequest,
    SignTransactionRequest,
)

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


class RewardClaimBodyDict(BaseModel):
    """Body sub-object from reward-distribution-data.json (dict / non-tuples variant)."""

    model_config = ConfigDict(populate_by_name=True)

    reward_epoch_id: int = Field(alias="rewardEpochId")
    beneficiary: str
    amount: str  # published as a decimal string
    claim_type: int = Field(alias="claimType")


class RewardClaimWithProofDict(BaseModel):
    """One element of rewardClaims[] in reward-distribution-data.json (dict variant)."""

    model_config = ConfigDict(populate_by_name=True)

    merkle_proof: list[str] = Field(alias="merkleProof")
    body: RewardClaimBodyDict


class RewardDistributionData(BaseModel):
    """reward-distribution-data.json (not tuples variant) — epoch id, merkle root + weight count."""

    model_config = ConfigDict(populate_by_name=True)

    reward_epoch_id: int = Field(alias="rewardEpochId")
    merkle_root: str = Field(alias="merkleRoot")
    no_of_weight_based_claims: int = Field(alias="noOfWeightBasedClaims")
    reward_claims: list[RewardClaimWithProofDict] = Field(
        alias="rewardClaims", default_factory=list
    )

    @field_validator("merkle_root")
    @classmethod
    def _validate_merkle_root(cls, v: str) -> str:
        if not _MERKLE_ROOT_RE.match(v):
            raise ValueError(f"merkleRoot must match ^0x[0-9a-fA-F]{{64}}$, got {v!r}")
        return v

    @field_validator("no_of_weight_based_claims")
    @classmethod
    def _validate_no_of_weight_based_claims(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"noOfWeightBasedClaims must be an integer >= 0, got {v}")
        return v
