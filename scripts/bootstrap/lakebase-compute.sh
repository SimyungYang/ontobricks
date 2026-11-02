#!/usr/bin/env bash
# ── OntoBricks: Lakebase compute settings ───────────────────────────────────
#
# Keeps the read-write (primary) endpoint of a Lakebase Autoscaling branch at
# the compute range and scale-to-zero timeout set in scripts/deploy.config.sh.
# deploy.sh runs it after the project/database step on every Lakebase deploy;
# only the fields that differ are updated, so a matching endpoint is untouched.
#
# Usage:
#   ./scripts/bootstrap/lakebase-compute.sh --project NAME --branch BRANCH \
#       [--min-cu N] [--max-cu N] [--suspend-timeout Ns] [--dry-run]
#
# Options:
#   --min-cu N            Autoscaling floor in CU (e.g. 0.5, 1, 4). Empty = keep.
#   --max-cu N            Autoscaling ceiling in CU (max - min <= 16). Empty = keep.
#   --suspend-timeout Ns  Idle time before scale-to-zero, in seconds with an
#                         "s" suffix (e.g. 3600s). Empty = keep.
#   --dry-run             Report the change without applying it.
#
# Uses the active Databricks CLI profile (DATABRICKS_CONFIG_PROFILE).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT=""
BRANCH=""
MIN_CU=""
MAX_CU=""
SUSPEND=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)         PROJECT="$2"; shift 2 ;;
    --branch)          BRANCH="$2";  shift 2 ;;
    --min-cu)          MIN_CU="$2";  shift 2 ;;
    --max-cu)          MAX_CU="$2";  shift 2 ;;
    --suspend-timeout) SUSPEND="$2"; shift 2 ;;
    --dry-run)         DRY_RUN=true; shift   ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

_log() { echo "[lakebase-compute] $*"; }
_err() { echo "[lakebase-compute] ERR $*" >&2; exit 1; }

[[ -z "$PROJECT" || -z "$BRANCH" ]] && _err "--project and --branch are required."

if [[ -z "$MIN_CU$MAX_CU$SUSPEND" ]]; then
  _log "No compute settings configured — leaving the endpoint as is."
  exit 0
fi

_is_cu() { [[ "$1" =~ ^[0-9]+(\.[0-9]+)?$ ]]; }
[[ -n "$MIN_CU" ]] && ! _is_cu "$MIN_CU" && _err "--min-cu '$MIN_CU' is not a number of CU."
[[ -n "$MAX_CU" ]] && ! _is_cu "$MAX_CU" && _err "--max-cu '$MAX_CU' is not a number of CU."
[[ -n "$SUSPEND" && ! "$SUSPEND" =~ ^[0-9]+s$ ]] && \
  _err "--suspend-timeout '$SUSPEND' must be seconds with an 's' suffix (e.g. 3600s)."
if [[ -n "$MIN_CU" && -n "$MAX_CU" ]]; then
  python3 -c "import sys; lo, hi = map(float, sys.argv[1:]); sys.exit(0 if lo <= hi <= lo + 16 else 1)" \
    "$MIN_CU" "$MAX_CU" || _err "CU range ${MIN_CU}-${MAX_CU} is invalid: need min <= max <= min + 16."
fi

BRANCH_PATH="projects/${PROJECT}/branches/${BRANCH}"
ENDPOINTS="$(databricks postgres list-endpoints "$BRANCH_PATH" -o json)" \
  || _err "Could not list endpoints of ${BRANCH_PATH}."

# Prints: endpoint name, then "mask<TAB>body" when an update is needed.
PLAN="$(printf '%s' "$ENDPOINTS" | python3 -c '
import json, sys
min_cu, max_cu, suspend = sys.argv[1:4]
data = json.load(sys.stdin)
eps = data if isinstance(data, list) else (data or {}).get("endpoints", [])
rw = [e for e in eps
      if ((e.get("status") or {}).get("endpoint_type") or (e.get("spec") or {}).get("endpoint_type"))
      == "ENDPOINT_TYPE_READ_WRITE"]
if not rw:
    sys.exit("no read-write endpoint")
ep = rw[0]
status = ep.get("status") or {}
wanted = {}
if min_cu:
    wanted["autoscaling_limit_min_cu"] = float(min_cu) if "." in min_cu else int(min_cu)
if max_cu:
    wanted["autoscaling_limit_max_cu"] = float(max_cu) if "." in max_cu else int(max_cu)
if suspend:
    wanted["suspend_timeout_duration"] = suspend
changed = {k: v for k, v in wanted.items() if status.get(k) != v}
print(ep["name"])
if changed:
    order = ["autoscaling_limit_min_cu", "autoscaling_limit_max_cu", "suspend_timeout_duration"]
    keys = [k for k in order if k in changed]
    print(",".join("spec." + k for k in keys) + "\t" + json.dumps({"spec": {k: changed[k] for k in keys}}))
' "$MIN_CU" "$MAX_CU" "$SUSPEND")" || _err "Could not read the read-write endpoint of ${BRANCH_PATH}."

ENDPOINT="$(printf '%s\n' "$PLAN" | sed -n 1p)"
CHANGE="$(printf '%s\n' "$PLAN" | sed -n 2p)"
WANT="min=${MIN_CU:-keep} max=${MAX_CU:-keep} suspend=${SUSPEND:-keep}"

if [[ -z "$CHANGE" ]]; then
  _log "${ENDPOINT} already at ${WANT}"
  exit 0
fi
MASK="${CHANGE%%$'\t'*}"
BODY="${CHANGE#*$'\t'}"

if $DRY_RUN; then
  _log "would set ${ENDPOINT}: ${BODY}"
  exit 0
fi

databricks postgres update-endpoint "$ENDPOINT" "$MASK" --json "$BODY" -o json >/dev/null \
  || _err "Failed to update ${ENDPOINT} (${MASK})."
_log "updated ${ENDPOINT}: ${BODY}"
