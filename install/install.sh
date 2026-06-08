#!/bin/sh
# clif installer — the FTSO reward AUTOMATION, deployed SEPARATELY from the fwd signer.
#
#   curl -sfL https://get.proofs.africa/clif | sudo sh -
#   git clone https://github.com/africanproofs/clif.git && sudo sh clif/install/install.sh
#
# Clones pinned source to /opt/clif, builds the clif image from source locally, and
# installs the `clifctl` host wrapper. clif is its OWN compose project (`clif`) with its
# own `egress` bridge; it joins the fwd signer's `internal: true` callers network as an
# `external` network to reach `fwd:8080`. clif holds ZERO private keys.
#
# Prerequisites:
#   - fwd is installed + running (it creates the `fwd_fwd-callers` network clif attaches to).
#   - `sudo fwd onboard rewards … --clif-env-dir /opt/clif` has written the per-network
#     .env.<net> files this deployment reads (caller tokens + wallet names; keyless).
#
# Config (env or flags):
#   CLIF_DIR=/opt/clif          install root          (--dir)
#   CLIF_BIN_DIR=/usr/local/bin host wrapper dir
#   CLIF_REPO=https://github.com/africanproofs/clif.git
#   CLIF_REF=main               git ref to build      (--ref)
#   FWD_NETWORK=fwd_fwd-callers fwd's external callers network
#   flags: --dir DIR --ref REF --no-build --help
set -eu

CLIF_DIR="${CLIF_DIR:-/opt/clif}"
CLIF_BIN_DIR="${CLIF_BIN_DIR:-/usr/local/bin}"
CLIF_REPO="${CLIF_REPO:-https://github.com/africanproofs/clif.git}"
CLIF_REF="${CLIF_REF:-main}"
FWD_NETWORK="${FWD_NETWORK:-fwd_fwd-callers}"
BUILD=1

log()  { printf '\033[1;33m[clif-install]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[clif-install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir)      shift; CLIF_DIR="${1:?--dir needs a value}" ;;
    --ref)      shift; CLIF_REF="${1:?--ref needs a value}" ;;
    --no-build) BUILD=0 ;;
    -h|--help)  sed -n '2,/^set -eu/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
    *)          die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# --- 1. preflight ---------------------------------------------------------
log "preflight"
have docker || die "docker not found — install Docker Engine first"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not found"
have git || die "git not found — needed to fetch pinned source"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon"

# --- 2. fetch pinned source ----------------------------------------------
mkdir -p "$CLIF_DIR"
if [ -d "$CLIF_DIR/.git" ]; then
  log "source present at $CLIF_DIR — fetching $CLIF_REF"
  git -C "$CLIF_DIR" fetch --depth 1 origin "$CLIF_REF" >/dev/null 2>&1 || die "git fetch failed"
  git -C "$CLIF_DIR" checkout -q -f FETCH_HEAD
elif [ -f "$CLIF_DIR/docker-compose.yml" ]; then
  log "using existing clif source at $CLIF_DIR (no .git — building in place)"
else
  # Empty, OR contains only operator config — e.g. the .env.<net> that
  # `fwd onboard --clif-env-dir $CLIF_DIR` writes BEFORE clif is installed.
  # `git clone` refuses a non-empty target, so init + fetch + checkout INTO the dir:
  # this lays the source down ALONGSIDE the gitignored .env.<net> files
  # (`checkout -f` overwrites tracked files only; it never touches untracked ones).
  log "fetching $CLIF_REPO @ $CLIF_REF -> $CLIF_DIR (clone-into-place; preserves .env.<net>)"
  git -C "$CLIF_DIR" init -q
  git -C "$CLIF_DIR" remote add origin "$CLIF_REPO" 2>/dev/null \
    || git -C "$CLIF_DIR" remote set-url origin "$CLIF_REPO"
  git -C "$CLIF_DIR" fetch --depth 1 origin "$CLIF_REF" >/dev/null 2>&1 \
    || die "git fetch failed: $CLIF_REPO (clif must be public)"
  git -C "$CLIF_DIR" checkout -q -f FETCH_HEAD
fi

# clif's compose validates env_file even for stopped services. Seed a placeholder .env
# from .env.example so `compose build` parses; the real per-network .env.<net> are written
# by `fwd onboard … --clif-env-dir $CLIF_DIR` (keyless caller tokens — never here).
[ -f "$CLIF_DIR/.env" ] || { [ -f "$CLIF_DIR/.env.example" ] && cp "$CLIF_DIR/.env.example" "$CLIF_DIR/.env" && log "seeded placeholder $CLIF_DIR/.env"; }

# --- 3. build the image from source --------------------------------------
export COMPOSE_PROJECT_NAME=clif FWD_NETWORK
if [ "$BUILD" -eq 1 ]; then
  log "building the clif image from source (slow first step)"
  ( cd "$CLIF_DIR" && docker compose --profile multichain build ) || die "docker compose build failed"
else
  log "--no-build: skipping image build"
fi

# Hand the dir to uid 1000 (the clif container user) so the non-root operator's host-side
# `docker compose` env_file reads of .env.<net> succeed.
[ "$(id -u)" = 0 ] && chown -R 1000:1000 "$CLIF_DIR" 2>/dev/null || true

# --- 4. install the clifctl host wrapper ---------------------------------
if [ -d "$CLIF_BIN_DIR" ] && [ -w "$CLIF_BIN_DIR" ]; then
  install -m 0755 "$CLIF_DIR/install/clifctl" "$CLIF_BIN_DIR/clifctl"
  # Bake the install-time CLIF_DIR + FWD_NETWORK defaults into the wrapper.
  sed -i "s#\${CLIF_DIR:-/opt/clif}#\${CLIF_DIR:-$CLIF_DIR}#" "$CLIF_BIN_DIR/clifctl" 2>/dev/null || true
  sed -i "s#\${FWD_NETWORK:-fwd_fwd-callers}#\${FWD_NETWORK:-$FWD_NETWORK}#" "$CLIF_BIN_DIR/clifctl" 2>/dev/null || true
  log "installed host wrapper: $CLIF_BIN_DIR/clifctl"
else
  log "NOTE: $CLIF_BIN_DIR not writable — run clifctl from $CLIF_DIR/install/clifctl"
fi

# --- 5. seed fwd nonces from chain truth (best-effort; non-fatal) ---------
# fwd is zero-egress: clif reads the on-chain tx count, the clifwd host wrapper writes fwd's
# nonce. Idempotent; no-ops cleanly if onboarding hasn't written .env.<net> yet (re-run later
# with `clifctl nonce-sync`). This restores the no-hand-typing nonce seeding without giving
# fwd egress.
_clifctl="$CLIF_BIN_DIR/clifctl"; [ -x "$_clifctl" ] || _clifctl="$CLIF_DIR/install/clifctl"
if [ "$BUILD" -eq 1 ]; then
  log "seeding fwd nonces from chain truth (clifctl nonce-sync)"
  ( env CLIF_DIR="$CLIF_DIR" FWD_NETWORK="$FWD_NETWORK" FWD_CONTAINER="${FWD_CONTAINER:-fwd}" "$_clifctl" nonce-sync ) \
    || log "nonce-sync incomplete — after fwd is onboarded + reachable, run: clifctl nonce-sync"
fi

cat <<EOF

clif is installed (separate from fwd; joins the '$FWD_NETWORK' network to reach fwd:8080).
next:
  1. (if not yet onboarded) sudo fwd onboard rewards --identity 0x… --recipient 0x… --networks songbird
       writes $CLIF_DIR/.env.<net>; then re-run this installer (or 'clifctl nonce-sync') to seed nonces.
  2. verify on-chain auth:  clifctl run songbird preflight --identity 0x… --recipient 0x…
  3. rehearse:              clifctl run songbird claim --type fee   (then verify the RewardClaimed event)
  4. enable (AFTER rehearsal): set FSP_AUTO_ENABLED=true in $CLIF_DIR/.env.songbird, then: clifctl up songbird
EOF
