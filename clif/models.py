"""Typed models: fsp-rewards distribution tuples + the fwd wire contract.

The fsp-rewards schema mirrors the upstream Zod schema in
`ftso-fee-claimer/src/interfaces.ts`. The fwd request/response models mirror
the fwd v1.1.0a9+ sign-only API:
  POST /v1/sign-transaction       -> SignTransactionResponse (signs; clif broadcasts)
  POST /v1/transactions/{id}/broadcast-result  -> BroadcastResultResponse
  POST /v1/transactions/{id}/receipt           -> ReceiptResponse
  GET  /v1/transactions/{id}     -> TxStatus (kept for any future use)
  POST /v1/sign-fsp-message      -> SignFspMessageResponse (Leg-1 unchanged)
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


# ---- fwd wire contract (fwd v1.1.0a9+ sign-only API) ----


class SignTransactionRequest(BaseModel):
    """POST /v1/sign-transaction request body.

    fwd signs the tx and returns the raw signed blob for clif to broadcast.
    fwd allocates the nonce; clif does NOT supply a nonce.
    gas, max_fee_per_gas, max_priority_fee_per_gas are computed by clif via
    rpc.py (estimate_gas + suggest_fees) before calling this endpoint.
    """

    wallet: str
    chain: int
    to: str
    value_wei: str = "0"
    data: str = "0x"
    gas: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


class SignTransactionResponse(BaseModel):
    """200 from POST /v1/sign-transaction.

    fwd signed the tx and computed its hash locally; it did NOT broadcast.
    `signed_raw_tx` is the 0x-prefixed RLP-encoded signed transaction that
    clif must pass to eth_sendRawTransaction.
    `hash` is the locally-computed tx hash (used to report back to fwd).
    """

    model_config = ConfigDict(extra="ignore")

    tx_id: str
    hash: str
    signed_raw_tx: str
    nonce: int


class BroadcastResultRequest(BaseModel):
    """POST /v1/transactions/{tx_id}/broadcast-result request body."""

    tx_hash: str
    outcome: str  # "accepted" | "rejected_releaseable" | "rejected_nonce_too_low"
    error_class: str | None = None


class BroadcastResultResponse(BaseModel):
    """200 from POST /v1/transactions/{tx_id}/broadcast-result."""

    model_config = ConfigDict(extra="ignore")

    tx_id: str
    status: str


class ReceiptRequest(BaseModel):
    """POST /v1/transactions/{tx_id}/receipt request body."""

    tx_hash: str
    outcome: str  # "mined_success" | "mined_reverted"
    block_number: int


class ReceiptResponse(BaseModel):
    """200 from POST /v1/transactions/{tx_id}/receipt."""

    model_config = ConfigDict(extra="ignore")

    tx_id: str
    status: str


class FwdError(BaseModel):
    model_config = ConfigDict(extra="ignore")

    error: str
    message: str


class TxStatus(BaseModel):
    """GET /v1/transactions/{tx_id} response (kept for completeness)."""

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
    reward_claims: list[RewardClaimWithProofDict] = Field(alias="rewardClaims", default_factory=list)

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
