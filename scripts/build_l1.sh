#!/bin/bash
# build_l1.sh [ninja targets] - inside a throwaway Aerial container (root), tree mounted at /opt/nvidia/cuBB (./dgist.sh build-l1).
# Incremental ninja build of $BUILD_DIR; FULL=1 (or a missing CMakeCache.txt) reconfigures with the validated flags. Output files -> tree owner.
set -eu
SDK=/opt/nvidia/cuBB; BUILD_DIR=${BUILD_DIR:-build.aarch64_f1002}; OWN=$(stat -c %u:%g $SDK); cd $SDK
T=${*:-cuphycontroller_scf dapp_tenant_main dapp_agent_test ru_emulator test_mac}
if [ "${FULL:-0}" = 1 ] || [ ! -f $BUILD_DIR/CMakeCache.txt ]; then
  echo "== configuring $BUILD_DIR"
  cmake -S $SDK -B $SDK/$BUILD_DIR -G Ninja -DCMAKE_BUILD_TYPE=Release -DSCF_FAPI_10_04=OFF -DENABLE_CONFORMANCE_TM_PDSCH_PDCCH=OFF -DSCF_FAPI_10_04_SRS=ON -DENABLE_DAPP_HOOK=ON -DBUILD_DOCS=OFF
fi
echo "== ninja -C $BUILD_DIR -j ${JOBS:-$(nproc)} $T"
nice -n 10 ninja -C $SDK/$BUILD_DIR -j ${JOBS:-$(nproc)} $T
grep -E "^(ENABLE_DAPP_HOOK|SCF_FAPI_10_04|ENABLE_CONFORMANCE_TM_PDSCH_PDCCH|SCF_FAPI_10_04_SRS):" $SDK/$BUILD_DIR/CMakeCache.txt
ls -la $SDK/$BUILD_DIR/cuPHY-CP/cuphycontroller/examples/cuphycontroller_scf $SDK/$BUILD_DIR/cuPHY-CP/dapp_hook/dapp_tenant_main
chown -R $OWN $SDK/$BUILD_DIR
echo "== build done"
