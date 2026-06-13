"""Consumer side of the fwd credential handoff (ADR-0001 / ADR-0003 —
`import-credentials`).

fwd emits a one-shot JSON **bundle** to a local host path (mode-0600, same-host
local trust, short TTL — NOT encrypted to a consumer key: a keyless consumer
holds no decryption key, ADR-0001 design point #4). clif imports it: validates
the bundle against the capabilities clif actually *requests* for that network
(`clif.config.capabilities`), writes the credentials into the per-network
`.env.<network>`, then CONSUMES (deletes) the bundle.

The bundle is now **v2 — the COMPLETE handoff** (consumer-contract-v1 §4 /
ADR-0003 Unit 4b). It carries, per capability, the bearer caller TOKEN and the
fwd WALLET NAME, plus a top-level `config` section holding the rest of clif's
non-secret `.env.<network>`. Importing a v2 bundle therefore sources clif's
ENTIRE env from the bundle — fwd no longer reads or writes clif's env (the
`--clif-env-dir` env-write is retired; Invariant #5 is closed). **v1 (tokens
only) is still accepted** for back-compat (fwd's host-side `env_write` supplied
the wallet-envs + config in the v1 era).

The token VALUES are bearer caller tokens (NOT signing keys) — importing them
keeps the keyless invariant intact; the `config` section carries no key (the
allowlist excludes every `*PRIVATE_KEY*` name and the env-injection guard is
applied to config values too). A token value is NEVER logged or echoed; only
capability_ids, counts and env-var NAMES are reported.

Import is **idempotent + re-runnable** — it is ALSO the rotation channel:
re-mint the same `capability_id` and re-import to replace the line in place.

NOTE: the v1/v2 bundle SHAPES are pinned by these validators and verified by
unit tests. End-to-end verification against a REAL fwd-emitted **v2** bundle is
PENDING — fwd's v2 bundle-emission side (Unit 4b) is the lockstep half and is
not yet deployed; the Songbird canary flips this to proven.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from clif.config import Settings, capabilities, config_env_allowlist

BUNDLE_VERSION = 2  # the COMPLETE handoff clif emits-against (tokens + wallet-envs + config)
SUPPORTED_VERSIONS = frozenset({1, 2})  # v1 (tokens-only) still imported for back-compat
CONSUMER = "claim"  # ADR-0004: the consumer identity is `claim` (the reward-harvester); was "clif"


class BundleError(ValueError):
    """A bundle is malformed, expired, for another consumer, or grants an
    ungoverned capability_id. Terminal — the operator re-issues the bundle."""


@dataclass(frozen=True)
class ImportedCredential:
    capability_id: str
    caller_token_env: str  # env var NAME the token was written under (NEVER the value)
    wallet_name: str | None
    wallet_env: str | None = None  # env var NAME the wallet name was written under (v2; None for v1)


@dataclass(frozen=True)
class ImportResult:
    network: str
    env_file: str
    imported: list[ImportedCredential]
    version: int = BUNDLE_VERSION
    # config env-var NAMES written (v2; empty for v1) — NAMES only, never values
    config_keys: list[str] = field(default_factory=list)

    @property
    def env_vars_written(self) -> list[str]:
        return [c.caller_token_env for c in self.imported]

    @property
    def wallet_envs_written(self) -> list[str]:
        return [c.wallet_env for c in self.imported if c.wallet_env]

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


def _is_clean(value: str) -> bool:
    """A value safe to write as a single `.env` line: no leading/trailing
    whitespace and no control/newline char. A newline would inject an extra
    `.env` assignment (env-injection); a control char writes a malformed line.
    Empty string is clean (a blank config value, e.g. `SIGNING_POLICY_ADDRESS=`,
    is legitimate)."""
    return value == value.strip() and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _validate_config_section(config: object, allowlist: frozenset[str]) -> None:
    """Validate a v2 bundle's `config` section against clif's own env-var allowlist.

    The v2 bundle sources clif's entire `.env.<network>`, so it MAY set config
    env-vars — but ONLY clif's own (an unknown key would inject an arbitrary env
    var) and never a secret/key. Raises ``BundleError`` (terminal) on a non-dict
    section, a `*PRIVATE_KEY*` key/value (Core #7 — clif holds no keys), an
    ungoverned key (not in the allowlist), a non-string value, or a value with
    illegal characters (env-injection). Never echoes a value.
    """
    if not isinstance(config, dict):
        raise BundleError("v2 bundle 'config' must be a JSON object")
    for key, value in config.items():
        if "PRIVATE_KEY" in str(key).upper() or (
            isinstance(value, str) and "PRIVATE_KEY" in value.upper()
        ):
            raise BundleError(
                f"config refuses key {key!r} — a *PRIVATE_KEY* name/value is forbidden "
                "(clif holds no keys, Core invariant #7)"
            )
        if key not in allowlist:
            raise BundleError(
                f"config has ungoverned key {key!r} — not a clif config env-var "
                f"(the bundle must not inject an arbitrary env var; allowed: {sorted(allowlist)})"
            )
        if not isinstance(value, str):
            raise BundleError(f"config key {key!r} value must be a string, got {type(value).__name__}")
        if not _is_clean(value):
            raise BundleError(f"config key {key!r} value contains illegal characters")


def validate_bundle(bundle: dict, settings: Settings) -> str:
    """Validate a parsed bundle against clif's governed capabilities for its network.

    Returns the validated network. Raises ``BundleError`` (terminal) on any
    violation: wrong version/consumer, expired, a malformed/ungoverned
    capability entry. ``settings.network`` is set to the bundle's network so the
    governed-id set is computed for the right chain.
    """
    if not isinstance(bundle, dict):
        raise BundleError("bundle root must be a JSON object")

    version = bundle.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise BundleError(
            f"unsupported bundle version {version!r} "
            f"(clif imports v{sorted(SUPPORTED_VERSIONS)}; emits-against v{BUNDLE_VERSION})"
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
        # A newline/control char in a value would inject extra `.env` assignments
        # (env-injection) or write a malformed line. Reject — never echo the value.
        if not _is_clean(token):
            raise BundleError(f"capability {cid!r} caller_token contains illegal characters")

        if version == 2:
            # v2 is the COMPLETE handoff: clif writes the wallet-env too, so every
            # capability MUST carry a clean, non-empty wallet_name (the value; the
            # env-var NAME comes from clif's own capability, never the bundle).
            wallet_name = entry.get("wallet_name")
            if not isinstance(wallet_name, str) or not wallet_name:
                raise BundleError(
                    f"v2 capability {cid!r} is missing its wallet_name (the COMPLETE handoff "
                    "writes the wallet-env; v1 supplied it via fwd's host-side env-write)"
                )
            if not _is_clean(wallet_name):
                raise BundleError(f"capability {cid!r} wallet_name contains illegal characters")

    if version == 2:
        _validate_config_section(bundle.get("config"), config_env_allowlist(settings))
        config = bundle.get("config") or {}
        config_network = config.get("NETWORK")
        if config_network is not None and config_network != network:
            raise BundleError(
                f"bundle config NETWORK {config_network!r} contradicts the "
                f"bundle's network {network!r}"
            )

    return network


def check_bundle_mode(path: Path) -> None:
    """Refuse a bundle that is not mode-0600 (consumer-contract-v1 §4.2 / C6).

    The bundle is the only artifact carrying plaintext caller-token VALUES outside
    fwd; it must not be group/other-accessible. Raises ``BundleError`` if any
    group/other permission bit is set. Reports the path + mode, never the contents.
    """
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise BundleError(
            f"bundle {path} is mode {oct(mode)}; must be 0600 (no group/other access)"
        )


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
    """Set ``NAME=value`` as the SINGLE canonical assignment (idempotent — the
    rotation channel). COLLAPSES every existing ``NAME=…`` line into one: pydantic
    reads the LAST assignment, so replacing only the first would leave a stale
    (revoked) token live. The canonical line takes the position of the first
    existing match (else is appended); all other lines + the trailing newline are
    preserved."""
    new_line = f"{name}={value}"
    out: list[str] = []
    placed = False
    for line in text.splitlines():
        if _is_assignment(line, name):
            if not placed:  # first match -> the canonical line; drop later duplicates
                out.append(new_line)
                placed = True
        else:
            out.append(line)
    if not placed:
        out.append(new_line)
    return "\n".join(out) + "\n"


def import_credentials(bundle: dict, settings: Settings, env_dir: Path) -> ImportResult:
    """Validate, write each token into ``<env_dir>/.env.<network>`` idempotently,
    and return what was written (NAMES + capability_ids only — never values).

    Does NOT consume (delete) the bundle — the caller does that AFTER a
    successful write, so a write failure leaves the one-shot bundle intact for a
    retry. Token values are never logged here.
    """
    network = validate_bundle(bundle, settings)
    version = bundle["version"]
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / f".env.{network}"
    text = env_file.read_text() if env_file.is_file() else ""

    governed = {c.capability_id: c for c in capabilities(settings)}
    imported: list[ImportedCredential] = []
    for entry in bundle["capabilities"]:
        cid = entry["capability_id"]
        env_name = entry["caller_token_env"]
        text = upsert_env_var(text, env_name, entry["caller_token"])
        wallet_env: str | None = None
        if version == 2:
            # COMPLETE handoff: also write the wallet-env. The NAME is clif's own
            # (governed[cid].wallet_env); the VALUE is the bundle's wallet_name.
            wallet_env = governed[cid].wallet_env
            text = upsert_env_var(text, wallet_env, entry["wallet_name"])
        imported.append(
            ImportedCredential(
                capability_id=cid,
                caller_token_env=env_name,
                wallet_name=entry.get("wallet_name") or governed[cid].wallet_name,
                wallet_env=wallet_env,
            )
        )

    # v2 config section: source the rest of clif's `.env.<network>` from the bundle.
    # Sorted for a deterministic write order (ADR-0003: diff determinism). NAMES
    # only are reported back — never the values.
    config_keys: list[str] = []
    if version == 2:
        for key, value in sorted(bundle.get("config", {}).items()):
            text = upsert_env_var(text, key, value)
            config_keys.append(key)

    # NETWORK is clif's only chain selector and silently defaults to flare when
    # absent (cli.py resolver; CLAUDE.md hard rule / D20). The import is the
    # authoritative env writer, so it ALWAYS pins NETWORK to the bundle's own
    # validated network — a handoff can no longer produce a wrong-chain env.
    text = upsert_env_var(text, "NETWORK", network)
    if "NETWORK" not in config_keys:
        config_keys.append("NETWORK")

    # mode-0600: the file holds bearer tokens. Set perms before/at write so the
    # token bytes never briefly sit world-readable.
    env_file.touch(mode=0o600, exist_ok=True)
    env_file.chmod(0o600)
    env_file.write_text(text)

    return ImportResult(
        network=network,
        env_file=str(env_file),
        imported=imported,
        version=version,
        config_keys=config_keys,
    )
