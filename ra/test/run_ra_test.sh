#!/bin/bash
# run_ra_test.sh (ICNL, 2026-11-11, test only): the Resource Allocator end to end without the real gNB.
#   nearRT-RIC + FlexRIC emulated gNB (test/emu: Slice SM that stores quotas / association, RC Style 2 floor, MAC report from
#   the UL simulator) + xapp_ccma_ra (E2 side, RC floors on) + ra_main.py + fake_dapp_agent.py + va_sim.py
#   + budgets from the PPO xApp in RA mode (case ppo) or a static budget (case static).
#
#   FR=<FlexRIC tree> [FB=build_dgist] run_ra_test.sh <case> <policy> [seconds] [extra va_sim args]
#     FR: a SCRATCH COPY of the package FlexRIC tree (openairinterface5g/openair2/E2AP/flexric), not the tree itself: copy test/emu/sm_{mac,rc,slice}.c
#         over its examples/emulator/agent/ and build it (FB = its build dir: nearRT-RIC, emu_agent_gnb, SM libraries). In the package tree
#         oai/update_patches.sh would take the emulator files into flexric_lab.patch. Needs ra/xapp/build/xapp_ccma_ra (../../ra.sh build).
#     case   ppo    : 2 VA + 2 Non-VA, budget from xapp_slice_ppo_ctrl (PPO_OUTPUT=ra, fake actor FAKE_PPO_VA, default 164)
#            static : 3 VA + 1 Non-VA, ra_main --static-budget 164:109 (the PPT demo layout)
#     policy ra | fixed:26x
set -u
FR=${FR:?FlexRIC tree (a scratch copy with test/emu built in)}; FB=${FB:-build_dgist}; D=${D:-$(cd "$(dirname "$0")/../.." && pwd)}; CASE=${1:?case}; POL=${2:?policy}; SECS=${3:-15}; shift 3 2>/dev/null; EXTRA="$*"
[ $CASE = ppo ] && : ${PPO_FAKE_BUNDLE:?case ppo needs the Slicing lab PPO xApp with PPO_OUTPUT=ra and its fake actor bundle (not in this repository)}
W=${W:-$D/runs/ra_test/$(date +%m%d_%H%M%S)_${CASE}_${POL/:/_}}; mkdir -p $W/sm $W/logs
find $FR/$FB/src/sm -name 'lib*_sm.so' -exec cp -f {} $W/sm/ \;
printf '[NEAR-RIC]\nNEAR_RIC_IP = 127.0.0.1\n\n[XAPP]\nDB_DIR = %s/\n' $W > $W/flexric.conf
if [ $CASE = ppo ]; then VA="4601:ue1,7a3c:ue2"; NV="b210,c0de"; else VA="4601:ue1,7a3c:ue2,9b21:ue3"; NV="c0de"; fi
VR=$(echo $VA | sed -E 's/:[a-z0-9]+//g'); ALL="$VR,$NV"
export EMU_RNTIS=$ALL EMU_STATE_FILE=$W/emu_state.json EMU_SIM_FILE=$W/sim_mac.json
export DAPP_CLIENT_DIR=${DAPP_CLIENT_DIR:-$D/aerial-cuda-accelerated-ran/cuPHY-CP/dapp_hook/python}
for p in 36421 36422; do ss -lnS | grep -q ":$p " && { echo "!! SCTP $p busy"; exit 1; }; done
for p in 9810; do ss -lnt | grep -q ":$p " && { echo "!! TCP $p busy"; exit 1; }; done
for p in 9820 9821 9822 9823 9824; do ss -lnu | grep -q ":$p " && { echo "!! UDP $p busy"; exit 1; }; done
pids=()
cleanup() {   # reverse start order: the RA first (on exit it removes its quotas through the E2 side, still up then)
  for ((i=${#pids[@]}-1; i>=0; i--)); do p=${pids[i]}; kill -TERM $p 2>/dev/null; for k in $(seq 1 10); do kill -0 $p 2>/dev/null || break; sleep 0.3; done; kill -KILL $p 2>/dev/null; done; }
trap cleanup EXIT
echo "== $CASE / $POL / ${SECS}s -> $W"
$FR/$FB/examples/ric/nearRT-RIC -c $W/flexric.conf -p $W/sm/ > $W/logs/ric.log 2>&1 & pids+=($!); sleep 1
$FR/$FB/examples/emulator/agent/emu_agent_gnb -c $W/flexric.conf -p $W/sm/ > $W/logs/emu.log 2>&1 & pids+=($!); sleep 2
python3 -u $D/ra/tools/fake_dapp_agent.py --sim-file $EMU_SIM_FILE > $W/logs/agent.log 2>&1 & pids+=($!); sleep 0.5
RA_RC_FLOORS=1 $D/ra/xapp/build/xapp_ccma_ra -c $W/flexric.conf -p $W/sm/ > $W/logs/ra_e2.log 2>&1 & pids+=($!); sleep 2
LABELS=$(echo $VA | sed -E 's/([0-9a-f]+):([a-z0-9]+)/\2=\1/g')
if [ $CASE = ppo ]; then
  python3 -u $D/ra/ra_main.py --agent 127.0.0.1:9810 --ue-label $LABELS --log-dir $W/ra > $W/logs/ra.log 2>&1 & pids+=($!); sleep 1
  mkdir -p $W/ppo/openairinterface5g/bin && cp -p $FR/$FB/examples/xApp/c/slice/xapp_slice_ppo_ctrl $W/ppo/openairinterface5g/bin/
  ( cd $W/ppo && FAKE_PPO_VA=${FAKE_PPO_VA:-164} PPO_OUTPUT=ra PPO_RADIO_INPUT=1 PPO_BUNDLE=$PPO_FAKE_BUNDLE PPO_PYTHON=python3 \
    VA_RNTIS=$(echo $VR | cut -d, -f1-2) NONVA_RNTIS=$NV exec $W/ppo/openairinterface5g/bin/xapp_slice_ppo_ctrl -c $W/flexric.conf -p $W/sm/ ) > $W/logs/ppo.log 2>&1 & pids+=($!)
  for i in $(seq 1 30); do grep -q 'READY' $W/ppo/log/ppo_output_log/*.log 2>/dev/null && break; sleep 0.5; done
else
  python3 -u $D/ra/ra_main.py --agent 127.0.0.1:9810 --ue-label $LABELS --static-budget 164:109 --va-rntis $VR --nonva-rntis $NV \
    --log-dir $W/ra > $W/logs/ra.log 2>&1 & pids+=($!); sleep 2
fi
python3 -u $D/ra/tools/va_sim.py --va $VA --nonva $NV --state-file $EMU_STATE_FILE --sim-file $EMU_SIM_FILE --policy $POL \
  --duration $SECS --out $W/va_sim_frames.csv --tag $CASE $EXTRA > $W/logs/va_sim.log 2>&1
cleanup; trap - EXIT
echo "-- quotas / association applied (emulated gNB):"; grep -E 'ADD rx|ASSOC rx' $W/logs/emu.log | sort | uniq -c | head -4
echo "-- RC floors received by the emulated gNB: $(grep -c 'EMU RC' $W/logs/emu.log) (nonzero $(grep 'EMU RC' $W/logs/emu.log | grep -vc 'floor 0 '))"
echo "-- RA:"; grep -E '^\[RA\] (E2 side|dApp|up)' $W/logs/ra.log | cut -c1-170; tail -2 $W/logs/ra.log | cut -c1-260
echo "-- E2 side:"; grep -E 'ready|stopping|FAILED' $W/logs/ra_e2.log | head -4
if [ $CASE = ppo ]; then L=$(ls $W/ppo/log/ppo_output_log/*.log | head -1); echo "-- PPO: READY $(grep -c READY $L) STORED $(grep -c STORED= $L) NOT_CONFIRMED $(grep -c NOT_CONFIRMED $L) HOLD $(grep -c HOLD $L)"; fi
echo "-- va_sim:"; grep -E 'MCS ->|policy|^   ue' $W/logs/va_sim.log
echo "-- ra_state status counts (budgets.csv):"; cut -d, -f7 $W/ra/budgets.csv | sort | uniq -c | tr '\n' ' '; echo
