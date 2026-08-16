"""Live delegation snapshot — what's staked with AP, on both the validator and FTSO.

Two independent delegation surfaces:
  • Validator (P-chain): AP's self-bond + the FLR delegated to its NodeID + delegator count + fee +
    lock end — from `platform.getCurrentValidators`.
  • FTSO (C-chain): the WNat vote power delegated to AP's FTSO delegation address (its wrapped-token
    delegations) — `WNat.votePowerOf(delegationAddress)`.

Read-only; holds no key.
"""

from __future__ import annotations

from clif.rpc import RpcClient, RpcError


def read_delegation(
    rpc: RpcClient, *, network: str, node_id: str | None, delegation_addr: str | None
) -> dict:
    """Snapshot of what's delegated to AP right now — validator + FTSO. Best-effort per leg (a read
    failure leaves that leg None; never raises)."""
    out: dict = {"network": network, "validator": None, "ftso": None}
    if node_id:
        try:
            out["validator"] = rpc.validator_delegation(node_id)
        except RpcError:
            pass
    if delegation_addr:
        try:
            wnat = rpc.contract_address_by_name("WNat")
            if wnat and int(wnat, 16) != 0:
                out["ftso"] = {
                    "delegation_addr": delegation_addr,
                    "vote_power": rpc.wnat_vote_power(wnat, delegation_addr) / 1e18,
                }
        except RpcError:
            pass
    return out
