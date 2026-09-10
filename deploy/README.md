# Local worker releases

`./deploy/deploy.sh worker` is the canonical **worker-only release of a previously
built and tested compatible image**, not a build command or permission to release
arbitrary builds. It dispatches before bootstrap configuration, secrets, resource
groups, ACR builds and Bicep. Calling `deploy.sh` without `worker` retains the
existing whole-environment bootstrap behavior; do not use bootstrap for an
ordinary worker release. API releases are independent.

Requires Python 3.9+ (stdlib only), `az` on PATH with an existing Azure login, and
an explicitly approved exclusive deployment window. Run from a trusted local
operator machine, **never as a private Azure deployment in public GitHub Actions**.
Check the current `az` subscription before applying. The script captures token
metadata internally and binds receipts to that subscription/resource identity.

```sh
# Read-only cloud preflight; no cloud/state writes. Image must already exist.
./deploy/deploy.sh worker --image thothacr21df07.azurecr.io/thoth-backend:0.3.122-ce92d05

# Prepare one private, durable directory per release; retain it for all resumes.
mkdir -m 700 "$HOME/worker-release-ce92d05"
./deploy/deploy.sh worker \
  --image thothacr21df07.azurecr.io/thoth-backend:0.3.122-ce92d05 \
  --expected-digest sha256:c28f87ad35c570151e242962e50421dfa0234110653ae1a719941aa69266d2c1 \
  --apply --exclusive-approved --state-dir "$HOME/worker-release-ce92d05"
```

The example is not a fresh CI/registry verification. Verify the chosen artifact's
build/test provenance before using it. Only fully qualified
`thothacr21df07.azurecr.io/thoth-backend:MAJOR.MINOR.PATCH-GITSHA` (7–40 lowercase
hex SHA characters), or `.../thoth-backend@sha256:<64 lowercase hex>` is accepted.
Aliases such as `latest` are rejected. A tag is resolved, locked and rechecked;
`--expected-digest` adds an independent artifact-identity check.

## Exact write and protection scope

- Read `thoth-backend-public`; **never PATCH the API**.
- Image-only template PATCH of exactly `thoth-sync-queued`,
  `thoth-sync-scheduled`, `thoth-payment-reminders` in `thoth-rg`, sequentially,
  all to the same supplied image. Requires one main container per Job. The full
  existing template is retained except its main container image; commands,
  arguments, env, resource limits, init containers, volumes and cron/configuration
  remain unchanged. Same-image targets are read/verified, not PATCHed.
- Resolve the candidate and previous **main-container images of these three
  target Jobs** using `az acr repository show`. Before any Job PATCH, set both
  `deleteEnabled=false` and `writeEnabled=false` on each available manifest using
  `az acr repository update --image thoth-backend@sha256:...`, and on each supplied
  version tag using `--image thoth-backend:...`; re-read each selector and verify
  digest plus both attributes and `readEnabled=true`. No delete, unlock or purge commands exist here.
- A missing *replaced prior reference* is a warning (reference hash + affected
  Job names), not a repair blocker. Only the registry's `MANIFEST_UNKNOWN` error
  or CLI exit 3 with the fixed `the specified tag does not exist.` sentence (case-insensitive, allowing CLI prefixes) qualifies;
  authentication, timeout and ambiguous errors fail closed.
  This permits repair of the retired `0.3.122-7c2d0ec` reference. Missing candidate,
  digest mismatch or unverified locks are fatal. Lock-stage errors are never
  treated as a missing-prior exemption. Missing references cannot be rollback
  targets; there is **no automatic rollback**.
- API images, non-target Jobs, init-container images and historic revisions not
  referenced by these three main containers are **not protected by this tool**.
  Inventory/protect those references in a separate authorized operation. Already
  protected rollback artifacts are never unlocked.
- No execution start/stop, DB, bank, sync, reminder-send or application HTTP
  commands. Normal schedules/event triggers can still run independently.

## Durable resume and limitations

The existing state directory must be owned by the operator and private (0700).
`flock` excludes cooperating writers using **that same local state directory**;
other directories/hosts/CI/portal writers are not excluded. The human-approved
exclusive window must cover registry locks through final postflight. This is not
an atomic cohort update, distributed CAS, or verified `If-Match` protocol.

Baseline and per-target receipts are exclusive-create, read-only (0400),
checksummed and fsynced, including their directory. They contain configuration
hashes, not raw templates/env/secrets. Checksums detect corruption and binding
mistakes, not malicious rewriting by the directory owner. Back up and retain
this directory; never remove/edit receipts to force a retry or switch state
directories to bypass an unresolved rollout.

Every receipt is durable **before** its PATCH. A timeout, disconnect or crash
(including a crash after receipt creation but before transmission) is ambiguous:
the script never retries that PATCH. Run the exact same command/state directory
to reconcile. A recorded Job must be read back as `Succeeded` with the target
image and unchanged nonimage settings before continuing; unsubmitted Jobs must
still match their original baselines. Unconfirmed/failed targets or any drift
exit nonzero, retaining all receipts for manual investigation. The script does
not refresh an expired token mid-run; safely resume with renewed credentials.

All three Jobs and the API are reread before each new PATCH and at final
postflight; the final ARM read before each PATCH is that target's fresh baseline.
Configuration hashing ignores only resource metadata (`id`, `name`, `type`,
`systemData`, `etag`) and read-only `provisioningState`, `runningStatus`,
`eventStreamEndpoint`; target identities and provisioning states are separately
checked. A service-added field can fail the comparison rather than silently
passing. API stable configuration, including images, must be exact.

Final paginated execution inventories report **added/removed identity hashes**,
not equality of counts and not a claim that no executions ran. Retention or
ordinary triggers can change those sets during the observation window.
`--timeout` bounds each CLI/HTTP call (default 60 seconds), `--poll-attempts` and
`--poll-interval` bound confirmation (24 × 5 seconds between reads by default),
and `--max-pages` bounds each inventory (default 100). Exhaustion fails closed.
These are per-call/loop bounds, not a whole-release wall-clock deadline.

## Offline checks

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_worker_release.py
bash -n deploy/deploy.sh
git diff --check
```

Tests use synthetic ARM responses, a fake `az` subprocess and temporary private
state directories (removed afterward). They do not authenticate, contact Azure,
start Jobs or access a bank. ARM PATCH/ACR behavior must still be reviewed against
current Azure documentation and verified by the authorized parent operator;
offline fixtures cannot establish live Azure service semantics.
