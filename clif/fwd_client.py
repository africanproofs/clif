"""Thin shim: re-exports the shared fwd-client library + clif's own idempotency helpers.

The transport (FwdClient, error classes, wire models) now lives in the shared
`fwd_client` package (github.com/africanproofs/fwd-client v0.1.0).  This shim
re-exports that surface so existing callers (`from clif.fwd_client import …`)
continue to work with no change.

clif keeps its own idempotency-key *composition* functions here:
  make_idempotency_key      — per logical reward claim (network/type/beneficiary/epoch)
  make_fsp_idempotency_key  — per FSP message (network/message_type/epoch/leg)

Both are reimplemented by composing via the lib's generic
`fwd_client.make_idempotency_key(*parts, retry=...)`, so the hashing
algorithm and 128-char cap come from the shared library.
"""

from __future__ import annotations

# Re-export the shared library's transport surface so callers use the same names.
from fwd_client import (  # noqa: F401
    FwdClient,
    FwdError,
    FwdRetryableError,
    FwdTerminalError,
    Health,
    SignTransactionResponse,
    BroadcastResultResponse,
    ReceiptResponse,
    SignFspMessageResponse,
    TxStatus,
    raise_for_fwd_error,
)
from fwd_client import make_idempotency_key as _lib_make_idempotency_key  # noqa: F401


def make_idempotency_key(
    network: str,
    claim_type: int,
    beneficiary: str,
    last_epoch_id: int,
    retry: str | None = None,
) -> str:
    """Deterministic per logical claim, with an explicit retry discriminator.

    A network retry / crash-rerun of the **same logical attempt** (same
    network, claim type, beneficiary, last epoch — and same `retry`) produces
    the **same** key, so fwd dedups instead of broadcasting twice (the
    double-claim safety property — must not regress).

    `retry` is the operator-controlled discriminator for a **deliberate**
    logical re-attempt after an on-chain failure (fwd replay is status-blind
    by design — fwd D14: a cached failed tx is pinned forever for its key).
    `retry=None` ⇒ the key is byte-identical to the legacy deterministic key.
    A new `retry` value ⇒ a fresh key. clif never auto-generates it.

    Hashing and 128-char cap delegated to fwd_client.make_idempotency_key.
    """
    return _lib_make_idempotency_key(
        f"clif:{network}:{claim_type}:{beneficiary.lower()}:{last_epoch_id}",
        retry=retry,
    )


def make_fsp_idempotency_key(
    network: str,
    message_type: str,
    reward_epoch_id: int,
    leg: str,
    retry: str | None = None,
) -> str:
    """Deterministic FSP idempotency key, ≤128 chars.

    Stable per (network, message_type, epoch, leg, retry). The `leg` param
    distinguishes Leg-1 (sign) from Leg-2 (submit) so each leg has an
    independent dedup window at fwd.

    Hashing and 128-char cap delegated to fwd_client.make_idempotency_key.
    """
    return _lib_make_idempotency_key(
        f"clif-fsp:{network}:{message_type}:{reward_epoch_id}:{leg}",
        retry=retry,
    )
