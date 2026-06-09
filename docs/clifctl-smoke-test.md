# clifctl smoke test — Slice-2 host surface

Operator-runnable validation of clif's **host wrapper** surface (`install/clifctl`)
and the two consumer self-check verbs (`clif status --json`, `clif doctor`). These
paths could **not** be runtime-tested in dev — they require a real Docker stack (fwd
up + a `clif-epoch-<net>` daemon on its `internal:true` callers network). This is the
checklist to run them once on a host that has the stack.

`<net>` = `flare` | `songbird` | `coston2`. Run every command from the fwd host (or
wherever `clifctl` is on `PATH` and `CLIF_DIR=/opt/clif`). Replace `<net>` with your
real network throughout.

---

## 0. Prerequisites (assert before any step)

| # | Precondition | How to confirm |
|---|---|---|
| P1 | **fwd is up** and its callers network exists | `docker network ls \| grep fwd_fwd-callers` returns a row; `docker ps \| grep '\bfwd\b'` shows the container running |
| P2 | **`.env.<net>` present** at `/opt/clif` | `ls /opt/clif/.env.<net>` exists (written by `sudo fwd onboard rewards … --clif-env-dir /opt/clif`) |
| P3 | **`FSP_AUTO_ENABLED=true`** in `.env.<net>` | `grep -i '^FSP_AUTO_ENABLED=true' /opt/clif/.env.<net>` matches — otherwise `clifctl up` warns and the daemon IDLES (signs nothing, status `disabled` → healthy) |
| P4 | **clif image built** | `clifctl build` has run, or `docker images \| grep clif` shows the tag |
| P5 | **daemon running** | `clifctl up <net>` returned 0 **and** `clifctl status <net>` shows the container `running` (P5 is the real gate — `up -d` returning 0 does not prove it stayed up) |

> If P3 is intentionally false (operator wants the daemon idle), `doctor`/`status`
> still pass: an idle daemon reports `disabled` → `degraded=false`, exit 0. The
> `disabled` summary line is healthy, **not** a failure.

---

## 1. `clifctl status <net>` — compose state + human epoch status

```sh
clifctl status <net>
```

**What it does:** `docker compose ps clif-epoch-<net>` followed by
`docker exec clif-epoch-<net> clif epoch status` (best-effort; `|| true`).

**Expected:**

- A compose `ps` row for `clif-epoch-<net>` with state `running` (or `Up …`).
- A human epoch line: `healthy` (green) — or, if P3 is false,
  `epoch daemon DISABLED (FSP_AUTO_ENABLED!=true) — idling, not signing`.
- Followed by `network=<net> last_done_epoch=… current_epoch=…` and zero or more
  `epoch N: <phase> — <detail>` lines.

**Note:** `clifctl status` itself does not exit non-zero on a degraded daemon here —
it is a human view (the `clif epoch status` exec is `|| true`). Use the `--json`
form (§2) or `doctor` (§3) when you need a **gating** exit code.

**PASS:** container `running` + a `healthy` (or `disabled`) epoch line.
**FAIL:** no `clif-epoch-<net>` row (daemon not up → fix P5), or an epoch line whose
summary starts `daemon status is stale …` / `DEGRADED: …`.

---

## 2. `clifctl status <net> --json` — machine-readable epoch status

```sh
clifctl status <net> --json
```

**What it does:** `docker exec clif-epoch-<net> clif epoch status --json` against the
**running** daemon (reads the daemon's status file inside the live container). Pure
JSON to stdout — no compose `ps`, no human lines.

**Expected JSON shape:**

```json
{
  "ok": true,
  "exit_code": 0,
  "summary": "healthy",
  "report": { "network": "<net>", "last_done_epoch": …, "current_epoch": …, "epochs": [ … ] }
}
```

**Expected exit codes:**

| exit | meaning | `ok` | `summary` (substring) |
|---|---|---|---|
| 0 | healthy (or `disabled`/idling) | `true` | `healthy` / `… DISABLED … idling, not signing` |
| 2 | degraded **or** daemon dead/stale | `false` | `DEGRADED: …` / `daemon status is stale (…s old > 3x…s) …` |
| 3 | no daemon state (daemon never wrote a report) | `false` | `no daemon status found (clif auto has not run)` |

> `exit 3` here means the **exec succeeded but the daemon has no status file yet** —
> distinct from `exit 2` (a written-but-unhealthy report). On a freshly-started
> daemon, allow one poll cycle before treating `3` as a failure.

**PASS:** `ok:true`, exit 0, `report.network == "<net>"`.
**FAIL:** exit 2/3 on a daemon expected healthy; or `docker exec` errors (container
not running → fix P5; this is a wrapper failure, not a clif exit code).

---

## 3. `clifctl doctor <net>` — one-shot consumer self-check

```sh
clifctl doctor <net>
```

**What it does:** `docker compose run --rm clif-epoch-<net> doctor` — a **one-shot**
container (does **not** require a running daemon; it probes fwd live and reads the
status file if present). Aggregates: keyless, fwd reachability, configured
capabilities (NAMES only), the compat tuple, and the daemon's status summary.

**Expected human output:**

```
clif doctor — <net> — OK
  keyless  : yes
  fwd      : http://fwd:8080 reachable=True master=ok
  clif/<net>/claim: configured=True
  clif/<net>/fsp-sign: configured=True
  clif/<net>/fsp-submit: configured=True
  daemon   : healthy        # or: "no daemon status found …" when run one-shot before a daemon exists
  compat   : fwd_contract=v1.1.0a69 fwd_client=… clif=0.5.35
```

**Expected exit codes:**

| exit | condition |
|---|---|
| 0 | fwd reachable + `master==ok` **and** the daemon status (if a report exists) is not failing |
| 2 | fwd unreachable / `master!=ok`, **or** a present daemon report is degraded/stale |

> **Daemon absence is not a failure.** `doctor` run one-shot (no daemon ever started)
> still exits 0 if fwd is healthy — `daemon.present=false` does not flip `ok`. Only a
> **present-and-failing** report (degraded/stale) contributes exit 2.

**PASS:** header `… — OK`, exit 0, all three `clif/<net>/{claim,fsp-sign,fsp-submit}`
lines `configured=True`, `fwd … reachable=True master=ok`.
**FAIL:** header `… — ISSUES`, exit 2 — read the `fwd` and `daemon` lines for which
half tripped.

---

## 4. `clifctl doctor <net> --json` — machine-readable scrape

```sh
clifctl doctor <net> --json
```

**What it does:** same self-check, machine-readable. This is the coordinator scrape
surface.

**Expected JSON shape** (exact keys):

```json
{
  "consumer": "clif",
  "network": "<net>",
  "ok": true,
  "keyless": true,
  "compat": {
    "fwd_contract_expected": "v1.1.0a69",
    "fwd_client": "…",
    "clif": "0.5.35"
  },
  "fwd": {
    "endpoint": "http://fwd:8080",
    "reachable": true,
    "master": "ok"
  },
  "capabilities": [
    { "capability_id": "clif/<net>/claim",      "role": "claim",      "configured": true },
    { "capability_id": "clif/<net>/fsp-sign",   "role": "fsp-sign",   "configured": true },
    { "capability_id": "clif/<net>/fsp-submit", "role": "fsp-submit", "configured": true }
  ],
  "daemon": {
    "present": true,
    "degraded": false,
    "summary": "healthy",
    "exit_code": 0
  }
}
```

Key contract (assert these are present):

- top-level: `consumer`, `network`, `ok`, `keyless`, `compat`, `fwd`, `capabilities`, `daemon`
- `compat{}`: `fwd_contract_expected`, `fwd_client`, `clif`
- `fwd{}`: `endpoint`, `reachable`, `master` — **plus `error`** (a string) only when unreachable
- each `capabilities[]` item: `capability_id`, `role`, `configured`
- `daemon{}`: `present`, `degraded`, `summary`, `exit_code`
  (`degraded` is `null` when `present:false`)

**Exit codes:** identical to §3 — **0** healthy / **2** fwd-unreachable-or-running-daemon-degraded.

**PASS:** `ok:true`, exit 0, `keyless:true`, the `claim` capability `configured:true`,
`fwd.reachable:true`, `fwd.master:"ok"`.

**FAIL — fwd down (the canonical negative case):** stop/break fwd, re-run:

```json
{ "ok": false, "fwd": { "endpoint": "http://fwd:8080", "reachable": false, "master": null, "error": "…" }, … }
```

exit **2**, `ok:false`, `fwd.reachable:false`. (Capabilities still render
`configured:true` — capability config is independent of fwd liveness; only `fwd` and
a failing `daemon` drive `ok`.)

---

## 5. Pass/fail matrix (the gating truth table)

| Scenario | `doctor --json` `ok` | doctor exit | `status --json` exit | Verdict |
|---|---|---|---|---|
| fwd up + daemon healthy + `FSP_AUTO_ENABLED=true` | `true` | 0 | 0 | **PASS** (claim cap `configured:true`) |
| fwd up + daemon idle (`FSP_AUTO_ENABLED` unset) | `true` | 0 | 0 | **PASS** (`disabled`/idling is healthy) |
| fwd up + doctor run one-shot, no daemon ever started | `true` | 0 | 3 (no daemon to exec) | **PASS for doctor**; `status --json` n/a until a daemon runs |
| fwd up + daemon **degraded/stale** | `false` | 2 | 2 | **FAIL** — read `daemon.summary` |
| **fwd down** (or `master!=ok`) | `false` | 2 | (status reads daemon file, may still be 0/2/3) | **FAIL** — `fwd.reachable:false` |

Minimum **green** smoke test (fwd + healthy daemon):

```sh
clifctl status <net>                              # container running + healthy/disabled line
clifctl status <net> --json   ; echo "exit=$?"    # ok:true, exit 0
clifctl doctor <net>                              # … — OK, three caps configured=True
clifctl doctor <net> --json   ; echo "exit=$?"    # ok:true, exit 0, keyless:true
```

All four green ⇒ Slice-2 host surface validated on this host.

---

## 6. Security note — no caller-token VALUE in any output

This is a hard invariant of the surface, and the smoke test must confirm it:

- `doctor` / `doctor --json` and `status` / `status --json` emit only env-var
  **NAMES** (`FWD_CALLER_TOKEN`, `FSP_SIGN_CALLER_TOKEN`, `FSP_SUBMIT_CALLER_TOKEN`)
  and a boolean `configured` — **never** the token value. `configured:true` means
  *clif holds a non-empty token in that env var*, nothing about its contents.
- No capability/compat/fwd/daemon field carries a secret. `fwd.endpoint` is an
  internal URL; `fwd.error` (unreachable case) is a transport message — neither
  contains a token.

**Confirm** (must print nothing):

```sh
clifctl doctor <net> --json | grep -iE '[0-9a-f]{32,}|secret|bearer|token.*[:=].*[A-Za-z0-9]{16,}'
```

Any match is a **STOP-SHIP leak** — do not proceed; report to the operator.
