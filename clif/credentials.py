"""Consumer side of the fwd credential handoff (ADR-0001 — `import-credentials`).

fwd emits a one-shot JSON **bundle** to a local host path (mode-0600, same-host
local trust, short TTL — NOT encrypted to a consumer key: a keyless consumer
holds no decryption key, ADR-0001 design point #4). clif imports it: validates
the bundle against the capabilities clif actually *requests* for that network
(`clif.config.capabilities`), writes each granted caller token into the
per-network `.env.<network>`, then CONSUMES (deletes) the bundle.

The token VALUES are bearer caller tokens clif already holds in its `.env`
(NOT signing keys) — importing them keeps the keyless invariant intact. A token
value is NEVER logged or echoed; only capability_ids, counts and env-var NAMES
are reported.

Import is **idempotent + re-runnable** — it is ALSO the rotation channel:
re-mint the same `capability_id` and re-import to replace the line in place.

NOTE: the bundle SHAPE is pinned to the v1 spec and verified by unit tests
against a hand-built fixture. End-to-end verification against a REAL
fwd-emitted bundle is PENDING — fwd's bundle-emission side is not yet built.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from clif.config import Settings, capabilities

BUNDLE_VERSION = 1
CONSUMER = "clif"


class BundleError(ValueError):
    """A bundle is malformed, expired, for another consumer, or grants an
    ungoverned capability_id. Terminal — the operator re-issues the bundle."""


@dataclass(frozen=True)
class ImportedCredential:
    capability_id: str
    caller_token_env: str  # env var NAME the value was written under (NEVER the value)
    wallet_name: str | None


@dataclass(frozen=True)
class ImportResult:
    network: str
    env_file: str
    imported: list[ImportedCredential]

    @property
    def env_vars_written(self) -> list[str]:
        return [c.caller_token_env for c in self.imported]

    @property
    def capability_ids(self) -> list[str]:
        return [c.capability_id for c in self.imported]


def _parse_iso8601(value: str, field: str) -> datetime:
    s = value.strip()
    if s.endswith("Z"):  # datetime.fromisoformat handles "Z" only on 3.11+, but be explicit
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise BundleError(f"bundle {field} is not a valid ISO8601 timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def validate_bundle(bundle: dict, settings: Settings) -> str:
    """Validate a parsed bundle against clif's governed capabilities for its network.

    Returns the validated network. Raises ``BundleError`` (terminal) on any
    violation: wrong version/consumer, expired, a malformed/ungoverned
    capability entry. ``settings.network`` is set to the bundle's network so the
    governed-id set is computed for the right chain.
    """
    if not isinstance(bundle, dict):
        raise BundleError("bundle root must be a JSON object")

    if bundle.get("version") != BUNDLE_VERSION:
        raise BundleError(
            f"unsupported bundle version {bundle.get('version')!r} (clif imports v{BUNDLE_VERSION})"
        )
    if bundle.get("consumer") != CONSUMER:
        raise BundleError(
            f"bundle consumer is {bundle.get('consumer')!r}, not {CONSUMER!r} — wrong consumer"
        )

    network = bundle.get("network")
    if not isinstance(network, str) or not network:
        raise BundleError("bundle is missing a 'network'")
    settings.network = network  # type: ignore[assignment]
    try:
        governed = {c.capability_id: c for c in capabilities(settings)}
    except KeyError as exc:  # unknown network -> _NETWORKS lookup fails
        raise BundleError(f"bundle network {network!r} is not one clif knows") from exc

    expires_at = bundle.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at:
        raise BundleError("bundle is missing 'expires_at'")
    if _parse_iso8601(expires_at, "expires_at") <= datetime.now(timezone.utc):
        raise BundleError(f"bundle expired at {expires_at} — re-issue the bundle")

    caps = bundle.get("capabilities")
    if not isinstance(caps, list) or not caps:
        raise BundleError("bundle has no 'capabilities' to import")

    for entry in caps:
        if not isinstance(entry, dict):
            raise BundleError("each capability entry must be a JSON object")
        cid = entry.get("capability_id")
        if cid not in governed:
            raise BundleError(
                f"bundle grants ungoverned capability_id {cid!r} — clif does not request it "
                f"on {network} (governed: {sorted(governed)})"
            )
        # The env var name must match the one clif actually reads for this id —
        # a mismatch would write a token clif never looks at.
        env_name = entry.get("caller_token_env")
        if env_name != governed[cid].caller_token_env:
            raise BundleError(
                f"capability {cid!r} caller_token_env {env_name!r} does not match clif's "
                f"expected {governed[cid].caller_token_env!r}"
            )
        token = entry.get("caller_token")
        if not isinstance(token, str) or not token:
            raise BundleError(f"capability {cid!r} is missing its caller_token value")

    return network


# A single env line: NAME or `export NAME` = anything. Used to find+replace idempotently.
def _is_assignment(line: str, name: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return False
    key = s.split("=", 1)[0].strip()
    if key.startswith("export "):
        key = key[len("export ") :].strip()
    return key == name


def upsert_env_var(text: str, name: str, value: str) -> str:
    """Replace the existing ``NAME=…`` line in ``text`` (idempotent — the rotation
    channel) or append it. Preserves all other lines and trailing newline policy."""
    lines = text.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if _is_assignment(line, name):
            lines[i] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def import_credentials(bundle: dict, settings: Settings, env_dir: Path) -> ImportResult:
    """Validate, write each token into ``<env_dir>/.env.<network>`` idempotently,
    and return what was written (NAMES + capability_ids only — never values).

    Does NOT consume (delete) the bundle — the caller does that AFTER a
    successful write, so a write failure leaves the one-shot bundle intact for a
    retry. Token values are never logged here.
    """
    network = validate_bundle(bundle, settings)
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / f".env.{network}"
    text = env_file.read_text() if env_file.is_file() else ""

    governed = {c.capability_id: c for c in capabilities(settings)}
    imported: list[ImportedCredential] = []
    for entry in bundle["capabilities"]:
        cid = entry["capability_id"]
        env_name = entry["caller_token_env"]
        text = upsert_env_var(text, env_name, entry["caller_token"])
        imported.append(
            ImportedCredential(
                capability_id=cid,
                caller_token_env=env_name,
                wallet_name=entry.get("wallet_name") or governed[cid].wallet_name,
            )
        )

    # mode-0600: the file holds bearer tokens. Set perms before/at write so the
    # token bytes never briefly sit world-readable.
    env_file.touch(mode=0o600, exist_ok=True)
    env_file.chmod(0o600)
    env_file.write_text(text)

    return ImportResult(network=network, env_file=str(env_file), imported=imported)
