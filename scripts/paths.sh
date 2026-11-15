# paths.sh - sourced by the package scripts right after .env ($D = the package directory). Nothing outside the package directory is needed:
# the .env paths below may be relative to it (./tgen/log); they are made absolute and exported here, so docker compose (a shell value wins
# over .env) and the containers get host paths. An absolute value is kept as it is (e.g. a log directory on another disk).
dg_abs() { case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s\n' "$D/${1#./}" ;; esac; }
export TRAFFIC_GEN_TV_DIR=$(dg_abs "${TRAFFIC_GEN_TV_DIR:-tgen/testVectors}")     # test vectors of the 5 emulator cells (scripts/fetch_testvectors.sh)
export TRAFFIC_GEN_LOG_DIR=$(dg_abs "${TRAFFIC_GEN_LOG_DIR:-tgen/log}")           # full nvlog files of a generator run (several GB per hour)
[ -n "${FLEXRIC_SM_DIR:-}" ] && export FLEXRIC_SM_DIR=$(dg_abs "$FLEXRIC_SM_DIR")   # E2 service models (./ric.sh install-sm); unset = /usr/local/lib/flexric for the gNB
true
