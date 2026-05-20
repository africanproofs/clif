"""HTTP transport for the fwd signing daemon (synchronous httpx).

Contract verified against `fwd/src/fwd/api/sign.py:38-161` this session:

  POST /v1/sign-and-send  -> 200 {tx_id,hash,nonce}
      errors {error,message}: 400 bad-req / bad-idempotency / chain_not_allowed
      401 unauthorized | 403 policy_denied | 404 wallet_not_found
      502 rpc_unreachable | 503 vault_unavailable
  GET  /v1/transactions/{tx_id}  -> {status,hashes,confirmed_at} (caller-gated)
  GET  /healthz                  -> {master,rpc,fwd}

Retry policy (fwd v1.0.0 taxonomy — Reviewer-binding):
  400/401/403/404 and 422 (transaction_rejected — the node deterministically
    refused, e.g. insufficient funds) are TERMINAL — never retry.
  502 (rpc_unreachable), 503 (sealed-master) and ANY httpx transport error
    (ConnectError/ReadTimeout/PoolTimeout/…) are RETRYABLE — a down or
    restarting fwd must degrade `clif auto`, never crash it.

This module is pure transport. It does not build claim calldata and does not
decide what to sign — that orchestration is `claimer.py` (Phase 8b step 4,
post operator gate).
"""

from __future__ import annotations

import hashlib
import re
import time

import httpx

from clif.models import Health, SignAndSendResponse, SignFspMessageResponse, TxStatus

_REWARDS_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

_TERMINAL_STATUSES = {400, 401, 403, 404, 422}
_RETRYABLE_STATUSES = {502, 503}
_TRANSPORT_ERROR_STATUS = 0  # synthetic: no HTTP response (down/restarting fwd)


class FwdError(RuntimeError):
    def __init__(self, status: int, error_code: str, message: str) -> None:
        super().__init__(f"fwd {status} {error_code}: {message}")
        self.status = status
        self.error_code = error_code
        self.message = message


class FwdTerminalError(FwdError):
    """Do not retry (auth/policy/wallet/bad-request/sealed-master)."""


class FwdRetryableError(FwdError):
    """May retry after backoff (RPC unreachable)."""


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
    """
    raw = f"clif-fsp:{network}:{message_type}:{reward_epoch_id}:{leg}"
    if retry:
        raw += f":retry={retry}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"clif-fsp-{network}-{message_type.lower()}-{reward_epoch_id}-{leg}-{digest[:16]}"


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

    Stable across processes; ≤128 chars (fwd's limit).
    """
    raw = f"clif:{network}:{claim_type}:{beneficiary.lower()}:{last_epoch_id}"
    if retry:
        raw += f":retry={retry}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"clif-{network}-{claim_type}-{last_epoch_id}-{digest[:16]}"


class FwdClient:
    def __init__(self, endpoint: str, caller_token: str | None, timeout: float = 60.0) -> None:
        self._base = endpoint.rstrip("/")
        self._token = caller_token
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FwdClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    @staticmethod
    def _raise_for_error(resp: httpx.Response) -> None:
        if resp.status_code == 200:
            return
        try:
            body = resp.json()
            err = str(body.get("error", "unknown"))
            msg = str(body.get("message", resp.text))
        except ValueError:
            err, msg = "unknown", resp.text
        if resp.status_code in _RETRYABLE_STATUSES:
            raise FwdRetryableError(resp.status_code, err, msg)
        if resp.status_code in _TERMINAL_STATUSES:
            raise FwdTerminalError(resp.status_code, err, msg)
        # Unmapped status: treat as terminal (fail closed).
        raise FwdTerminalError(resp.status_code, err, msg)

    def _transport_retryable(self, exc: httpx.RequestError) -> FwdRetryableError:
        return FwdRetryableError(
            _TRANSPORT_ERROR_STATUS,
            "transport_error",
            f"{type(exc).__name__}: {exc}",
        )

    def health(self) -> Health:
        try:
            resp = self._client.get(f"{self._base}/healthz")
        except httpx.RequestError as exc:
            raise self._transport_retryable(exc) from exc
        resp.raise_for_status()
        return Health.model_validate(resp.json())

    def sign_and_send(
        self,
        wallet: str,
        chain: int,
        to: str,
        data: str = "0x",
        value_wei: str = "0",
        gas: int | None = None,
        idempotency_key: str | None = None,
    ) -> SignAndSendResponse:
        payload: dict = {
            "wallet": wallet,
            "chain": chain,
            "to": to,
            "value_wei": value_wei,
            "data": data,
        }
        if gas is not None:
            payload["gas"] = gas
        headers = dict(self._auth)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            resp = self._client.post(
                f"{self._base}/v1/sign-and-send", json=payload, headers=headers
            )
        except httpx.RequestError as exc:
            raise self._transport_retryable(exc) from exc
        self._raise_for_error(resp)
        return SignAndSendResponse.model_validate(resp.json())

    def sign_fsp_message(
        self,
        wallet: str,
        message_type: str,
        reward_epoch_id: int,
        *,
        chain_id: int | None = None,
        no_of_weight_based_claims: int | None = None,
        rewards_hash: str | None = None,
        idempotency_key: str | None = None,
    ) -> SignFspMessageResponse:
        """POST /v1/sign-fsp-message — Leg-1 of the FSP signing-tool path.

        fwd signs the FSP message (UPTIME or REWARD_DISTRIBUTION) and returns
        (message_hash, v, r, s, signature). clif never sees a key. Leg-2 is
        the existing sign_and_send to FlareSystemsManager with the built calldata.

        Cross-field rules (fail-loud before any HTTP — D14):
        - UPTIME: chain_id / no_of_weight_based_claims / rewards_hash must all be None.
        - REWARD_DISTRIBUTION: all three must be present; rewards_hash must match
          ^0x[0-9a-fA-F]{64}$.
        - Unknown message_type: ValueError.
        """
        rd_fields = (chain_id, no_of_weight_based_claims, rewards_hash)
        if message_type == "UPTIME":
            if any(f is not None for f in rd_fields):
                raise ValueError(
                    "UPTIME: chain_id / no_of_weight_based_claims / rewards_hash must all be None"
                )
        elif message_type == "REWARD_DISTRIBUTION":
            if any(f is None for f in rd_fields):
                raise ValueError(
                    "REWARD_DISTRIBUTION: chain_id, no_of_weight_based_claims, "
                    "and rewards_hash are all required"
                )
            assert rewards_hash is not None  # type narrowing for mypy
            if not _REWARDS_HASH_RE.match(rewards_hash):
                raise ValueError(
                    f"rewards_hash must match ^0x[0-9a-fA-F]{{64}}$, got {rewards_hash!r}"
                )
        else:
            raise ValueError(f"Unknown message_type {message_type!r}; expected UPTIME or REWARD_DISTRIBUTION")

        payload: dict = {
            "wallet": wallet,
            "message_type": message_type,
            "reward_epoch_id": reward_epoch_id,
        }
        if message_type == "REWARD_DISTRIBUTION":
            payload["chain_id"] = chain_id
            payload["no_of_weight_based_claims"] = no_of_weight_based_claims
            payload["rewards_hash"] = rewards_hash

        headers = dict(self._auth)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        try:
            resp = self._client.post(
                f"{self._base}/v1/sign-fsp-message", json=payload, headers=headers
            )
        except httpx.RequestError as exc:
            raise self._transport_retryable(exc) from exc
        self._raise_for_error(resp)
        return SignFspMessageResponse.model_validate(resp.json())

    def get_transaction(self, tx_id: str) -> TxStatus:
        try:
            resp = self._client.get(
                f"{self._base}/v1/transactions/{tx_id}", headers=self._auth
            )
        except httpx.RequestError as exc:
            raise self._transport_retryable(exc) from exc
        self._raise_for_error(resp)
        return TxStatus.model_validate(resp.json())

    def wait_until_mined(
        self, tx_id: str, timeout: float = 600.0, poll: float = 5.0
    ) -> TxStatus:
        deadline = time.monotonic() + timeout
        while True:
            st = self.get_transaction(tx_id)
            if st.status in ("mined", "failed", "replaced", "dropped"):
                return st
            if time.monotonic() >= deadline:
                raise TimeoutError(f"tx {tx_id} not terminal after {timeout}s (status={st.status})")
            time.sleep(poll)
