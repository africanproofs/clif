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

_CB58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _bytes20_to_cb58(b: bytes) -> str:
    """Encode 20-byte node ID as Avalanche CB58 (sha256[-4:] checksum, base58)."""
    import hashlib
    checksum = hashlib.sha256(b).digest()[-4:]
    payload = b + checksum
    n = int.from_bytes(payload, "big")
    result: list[bytes] = []
    while n > 0:
        n, r = divmod(n, 58)
        result.append(_CB58_ALPHABET[r : r + 1])
    leading = sum(1 for byte in payload if byte == 0)
    result.extend([b"1"] * leading)
    return b"".join(reversed(result)).decode()


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

    def _abi_decode(self, types: list, data: bytes) -> tuple:
        try:
            return abi_decode(types, data)
        except Exception as exc:
            raise RpcError(f"abi decode failed {types}: {exc}") from exc

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

    def get_transaction_count(self, address: str, block_tag: str = "latest") -> int:
        """eth_getTransactionCount(address, block_tag) → next nonce (int).

        block_tag "latest" = mined count; "pending" = incl. mempool. Keyless read
        (address is public; no signing). Raises RpcError on transport/JSON-RPC error
        (same as the other read methods).
        """
        result = self._call("eth_getTransactionCount", [address, block_tag])
        return int(str(result), 16)

    # ---- typed view helpers (keyless) ----

    def rewards_hash(self, flare_systems_manager: str, epoch_id: int) -> str:
        data = "0x" + selector("rewardsHash(uint256)").hex() + abi_encode(
            ["uint256"], [epoch_id]
        ).hex()
        (out,) = self._abi_decode(["bytes32"], self.eth_call(flare_systems_manager, data))
        return "0x" + out.hex()

    def next_claimable_reward_epoch_id(self, reward_manager: str, owner: str) -> int:
        data = "0x" + selector("getNextClaimableRewardEpochId(address)").hex() + abi_encode(
            ["address"], [owner]
        ).hex()
        (out,) = self._abi_decode(["uint256"], self.eth_call(reward_manager, data))
        return int(out)

    def reward_epoch_id_range(self, reward_manager: str) -> tuple[int, int]:
        data = "0x" + selector("getRewardEpochIdsWithClaimableRewards()").hex()
        start, end = self._abi_decode(["uint24", "uint24"], self.eth_call(reward_manager, data))
        return int(start), int(end)

    def get_current_reward_epoch_id(self, flare_systems_manager: str) -> int:
        """Read getCurrentRewardEpochId() → uint24 from FlareSystemsManager (keyless)."""
        # Selector: keccak256("getCurrentRewardEpochId()")[:4] = 0x70562697 (verified anchor)
        data = "0x" + selector("getCurrentRewardEpochId()").hex()
        (out,) = self._abi_decode(["uint24"], self.eth_call(flare_systems_manager, data))
        return int(out)

    def claim_executors(self, claim_setup_manager: str, owner: str) -> list[str]:
        """claimExecutors(address) → address[] — who can claim on behalf of owner."""
        data = "0x" + selector("claimExecutors(address)").hex() + abi_encode(["address"], [owner]).hex()
        (out,) = self._abi_decode(["address[]"], self.eth_call(claim_setup_manager, data))
        return [str(a) for a in out]

    def allowed_claim_recipients(self, claim_setup_manager: str, owner: str) -> list[str]:
        """allowedClaimRecipients(address) → address[] — allow-listed recipient addresses."""
        data = "0x" + selector("allowedClaimRecipients(address)").hex() + abi_encode(["address"], [owner]).hex()
        (out,) = self._abi_decode(["address[]"], self.eth_call(claim_setup_manager, data))
        return [str(a) for a in out]

    def get_balance(self, address: str) -> int:
        """eth_getBalance(address) → wei (int)."""
        result = self._call("eth_getBalance", [address, "latest"])
        return int(str(result), 16)

    def get_voter_addresses(self, entity_manager: str, voter: str) -> tuple[str, str, str]:
        """getVoterAddresses(address) → (submitAddress, submitSignaturesAddress, signingPolicyAddress)."""
        data = "0x" + selector("getVoterAddresses(address)").hex() + abi_encode(["address"], [voter]).hex()
        sa, ssa, spa = self._abi_decode(["address", "address", "address"], self.eth_call(entity_manager, data))
        return str(sa), str(ssa), str(spa)

    def get_delegation_address(self, entity_manager: str, voter: str) -> str:
        """getDelegationAddressOf(address) → address."""
        data = "0x" + selector("getDelegationAddressOf(address)").hex() + abi_encode(["address"], [voter]).hex()
        (da,) = self._abi_decode(["address"], self.eth_call(entity_manager, data))
        return str(da)

    def get_node_ids(self, entity_manager: str, voter: str) -> list[str]:
        """getNodeIdsOf(address) → bytes20[] as 'NodeID-<CB58>' strings."""
        data = "0x" + selector("getNodeIdsOf(address)").hex() + abi_encode(["address"], [voter]).hex()
        (ids,) = self._abi_decode(["bytes20[]"], self.eth_call(entity_manager, data))
        return [f"NodeID-{_bytes20_to_cb58(bytes(b))}" for b in ids]

    def uptime_vote_hash(self, flare_systems_manager: str, epoch_id: int) -> str:
        """Read uptimeVoteHash(uint256) → bytes32 from FlareSystemsManager.

        Returns the 0x-prefixed bytes32.  Zero (ZERO_BYTES32) = the uptime vote
        for this epoch has NOT yet been finalized (the >50% threshold not reached).
        Non-zero = uptime voting has finalized for this epoch (analogous to
        rewardsHash for REWARD_DISTRIBUTION).
        """
        data = "0x" + selector("uptimeVoteHash(uint256)").hex() + abi_encode(
            ["uint256"], [epoch_id]
        ).hex()
        (out,) = self._abi_decode(["bytes32"], self.eth_call(flare_systems_manager, data))
        return "0x" + out.hex()

    def get_revert_reason(self, tx_hash: str) -> str | None:
        """Attempt to decode the revert reason for a mined-reverted tx by replaying it.

        Fetches the tx (from/to/input/value) and receipt (blockNumber), then
        replays via eth_call at that block.  The node returns an error whose
        ``data`` (or ``message``) carries the ABI-encoded revert; we decode
        ``Error(string)`` when ``data`` starts with selector ``0x08c379a0``
        (after that 4-byte selector: 32-byte offset, 32-byte length, then the
        UTF-8 reason string).

        Returns the reason string on success, or ``None`` if it cannot be
        determined (non-archival node, empty data, unexpected encoding, any RPC
        failure).  Never raises — callers fall back to generic terminal
        classification on ``None``.
        """
        try:
            tx = self.get_transaction_by_hash(tx_hash)
            if tx is None:
                return None
            receipt = self.get_transaction_receipt(tx_hash)
            if receipt is None:
                return None
            block_number = receipt.get("blockNumber")
            if block_number is None:
                return None
            # Build eth_call params mirroring the original tx.
            call_obj: dict = {
                "to": tx.get("to") or "",
                "data": tx.get("input") or "0x",
            }
            if tx.get("from"):
                call_obj["from"] = tx["from"]
            value = tx.get("value")
            if value and value not in ("0x0", "0x", "0"):
                call_obj["value"] = value
            # Replay at the mined block — requires an archival node.
            self._id += 1
            try:
                resp = self._client.post(
                    self._url,
                    json={
                        "jsonrpc": "2.0",
                        "id": self._id,
                        "method": "eth_call",
                        "params": [call_obj, block_number],
                    },
                )
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPError, ValueError):
                return None
            # The node may surface the revert in the error object or in the result.
            raw_data: str | None = None
            if "error" in body:
                err = body["error"]
                if isinstance(err, dict):
                    raw_data = err.get("data") or err.get("message") or ""
                elif isinstance(err, str):
                    raw_data = err
            elif "result" in body:
                # Some nodes return the revert data as the result of eth_call.
                raw_data = str(body.get("result") or "")
            if not raw_data:
                return None
            # Decode Error(string): selector 0x08c379a0 + abi.encode(string).
            # Strip optional "Reverted " / "execution reverted: " prefixes.
            prefix = ""
            for pfx in ("Reverted 0x", "execution reverted: 0x", "0x"):
                if raw_data.startswith(pfx) or raw_data.lower().startswith(pfx.lower()):
                    prefix = pfx
                    break
            hex_data = raw_data[len(prefix):].strip()
            if not hex_data.startswith("08c379a0"):
                # Some nodes embed the plain text in the message field.
                for pfx in ("execution reverted: ", "Reverted "):
                    if raw_data.startswith(pfx):
                        return raw_data[len(pfx):].strip()
                return None
            data_bytes = bytes.fromhex(hex_data)
            # data_bytes = selector(4) + abi.encode(string)
            # abi.encode(string) = 32-byte offset + 32-byte length + padded utf-8
            if len(data_bytes) < 4 + 32 + 32:
                return None
            payload = data_bytes[4:]  # strip selector
            (reason,) = abi_decode(["string"], payload)
            return str(reason)
        except Exception:  # noqa: BLE001
            return None
