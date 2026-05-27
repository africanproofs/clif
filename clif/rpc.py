"""Keyless JSON-RPC client (httpx, synchronous).

Read paths (existing): `eth_call` for the FtsoRewardManager /
FlareSystemsManager view functions used during discovery, plus
`eth_getTransactionReceipt` for on-chain verification.

Write paths (new, broadcast-only — no signing, no key):
  `send_raw_transaction` — broadcasts a fwd-signed blob via
    `eth_sendRawTransaction`. Accepting a fwd-signed blob is NOT signing:
    clif never constructs or holds private keys.

Fee estimation (new, keyless reads):
  `estimate_gas` — `eth_estimateGas` with a 25% buffer.
  `suggest_fees` — `eth_feeHistory` → (max_fee_per_gas, max_priority_fee_per_gas).

Receipt polling:
  `poll_receipt` — bounded `eth_getTransactionReceipt` loop (keyless read).

Synchronous by design: the signing path is short and sequential; an event
loop would add plumbing without benefit (Behavioural guideline 2).
"""

from __future__ import annotations

import time
from typing import Any, cast

import httpx
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from clif.calldata import selector

# Gas estimation buffer: 25% over the eth_estimateGas result.
_GAS_BUFFER = 1.25
# Tip: 1 gwei (in wei) — conservative default.
_DEFAULT_TIP_WEI = 1_000_000_000
# Sanity ceiling: if computed max_fee exceeds this, cap it.
# Keeps clif comfortably under fwd's FWD_MAX_FEE_PER_GAS default (500 gwei).
_MAX_FEE_CAP_WEI = 300_000_000_000  # 300 gwei
_MAX_GAS_CAP = 10_000_000  # 10M — well under fwd's FWD_MAX_GAS default (15M)


class RpcError(RuntimeError):
    pass


class RpcClient:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self._url = url
        self._client = httpx.Client(timeout=timeout)
        self._id = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RpcClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _call(self, method: str, params: list) -> object:
        self._id += 1
        try:
            resp = self._client.post(
                self._url,
                json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RpcError(f"{method} transport failure: {exc}") from exc
        if "error" in body:
            raise RpcError(f"{method} rpc error: {body['error']}")
        return body["result"]

    # ---- write path (broadcast only — no key, no signing) ----

    def send_raw_transaction(self, signed_raw_tx: str) -> str:
        """Broadcast a fwd-signed raw tx blob via eth_sendRawTransaction.

        Returns the tx hash (0x-prefixed) as returned by the node.
        Raises RpcError on any node rejection (insufficient funds, nonce too
        low, etc.) — callers are responsible for classifying the error into
        the fwd broadcast-result outcome taxonomy.
        """
        result = self._call("eth_sendRawTransaction", [signed_raw_tx])
        return str(result)

    # ---- fee estimation (keyless reads) ----

    def estimate_gas(self, from_addr: str, to: str, data: str, value_wei: int = 0) -> int:
        """eth_estimateGas with a 25% buffer, capped at _MAX_GAS_CAP.

        `from_addr` is the fwd-custodied sender wallet address (public info,
        not a key). The node uses it for the simulation; without it, some
        contract calls fail (wrong msg.sender). Clif passes the configured
        wallet address, which is public.
        """
        params: dict = {
            "from": from_addr,
            "to": to,
            "data": data,
        }
        if value_wei:
            params["value"] = hex(value_wei)
        result = self._call("eth_estimateGas", [params, "latest"])
        raw = int(str(result), 16)
        buffered = int(raw * _GAS_BUFFER)
        return min(buffered, _MAX_GAS_CAP)

    def suggest_fees(self) -> tuple[int, int]:
        """eth_feeHistory → (max_fee_per_gas, max_priority_fee_per_gas) in wei.

        Strategy: baseFee × 2 + tip (1 gwei), capped at _MAX_FEE_CAP_WEI.
        Returns (max_fee_per_gas, max_priority_fee_per_gas).
        """
        result = self._call("eth_feeHistory", [4, "latest", []])
        history: dict[str, Any] = cast(dict, result)
        base_fees: list[Any] = history.get("baseFeePerGas", [])
        # Take the latest base fee (last element in the list is the pending block).
        if base_fees:
            latest_base = int(str(base_fees[-1]), 16)
        else:
            latest_base = _DEFAULT_TIP_WEI  # fallback

        tip = _DEFAULT_TIP_WEI
        max_fee = min(latest_base * 2 + tip, _MAX_FEE_CAP_WEI)
        # max_priority must not exceed max_fee
        max_priority = min(tip, max_fee)
        return max_fee, max_priority

    # ---- receipt polling (keyless read) ----

    def poll_receipt(
        self,
        tx_hash: str,
        timeout: float = 600.0,
        poll: float = 5.0,
    ) -> dict | None:
        """Poll eth_getTransactionReceipt until mined or timeout.

        Returns the receipt dict on success, or None on timeout (tx still
        pending). Raises RpcError on transport failures.
        """
        deadline = time.monotonic() + timeout
        while True:
            receipt = self.get_transaction_receipt(tx_hash)
            if receipt is not None:
                return receipt
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll)

    def eth_call(self, to: str, data: str) -> bytes:
        result = self._call("eth_call", [{"to": to, "data": data}, "latest"])
        return bytes.fromhex(str(result)[2:])

    def get_transaction_receipt(self, tx_hash: str) -> dict | None:
        result = self._call("eth_getTransactionReceipt", [tx_hash])
        return cast(dict, result) if result else None  # null until mined

    def get_transaction_by_hash(self, tx_hash: str) -> dict | None:
        """The mined tx (incl. on-chain `from`) — the fwd-custody proof read.

        `from` is the secp256k1-recovered sender: it equals the fwd-custodied
        executor wallet iff fwd signed. clif never signs, so this is how the
        rehearsal proves the custody path end-to-end.
        """
        result = self._call("eth_getTransactionByHash", [tx_hash])
        return cast(dict, result) if result else None  # null until propagated

    # ---- typed view helpers (keyless) ----

    def rewards_hash(self, flare_systems_manager: str, epoch_id: int) -> str:
        data = "0x" + selector("rewardsHash(uint256)").hex() + abi_encode(
            ["uint256"], [epoch_id]
        ).hex()
        (out,) = abi_decode(["bytes32"], self.eth_call(flare_systems_manager, data))
        return "0x" + out.hex()

    def next_claimable_reward_epoch_id(self, reward_manager: str, owner: str) -> int:
        data = "0x" + selector("getNextClaimableRewardEpochId(address)").hex() + abi_encode(
            ["address"], [owner]
        ).hex()
        (out,) = abi_decode(["uint256"], self.eth_call(reward_manager, data))
        return int(out)

    def reward_epoch_id_range(self, reward_manager: str) -> tuple[int, int]:
        data = "0x" + selector("getRewardEpochIdsWithClaimableRewards()").hex()
        start, end = abi_decode(["uint24", "uint24"], self.eth_call(reward_manager, data))
        return int(start), int(end)

    def get_current_reward_epoch_id(self, flare_systems_manager: str) -> int:
        """Read getCurrentRewardEpochId() → uint24 from FlareSystemsManager (keyless)."""
        # Selector: keccak256("getCurrentRewardEpochId()")[:4] = 0x70562697 (verified anchor)
        data = "0x" + selector("getCurrentRewardEpochId()").hex()
        (out,) = abi_decode(["uint24"], self.eth_call(flare_systems_manager, data))
        return int(out)
