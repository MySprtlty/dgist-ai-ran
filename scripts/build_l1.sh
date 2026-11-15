#!/bin/bash
# build_l1.sh [ninja targets] - inside a throwaway Aerial container (root), tree mounted at /opt/nvidia/cuBB (./dgist.sh build-l1).
# Incremental ninja build of $BUILD_DIR; FULL=1 (or a build directory without build.ninja: a fresh clone, an interrupted configure) configures with
# the validated flags and the toolchain file cuPHY/cmake/toolchains/$TOOLCHAIN (.env L1_TOOLCHAIN; grace-cross = GH200). Output files -> tree owner.
set -eu
SDK=/opt/nvidia/cuBB; BUILD_DIR=${BUILD_DIR:-build.aarch64_f1002}; TOOLCHAIN=${TOOLCHAIN:-grace-cross}; OWN=$(stat -c %u:%g $SDK); cd $SDK
trap 'chown -R $OWN $SDK/$BUILD_DIR 2>/dev/null' EXIT   # also after a failed configure / build (this container runs as root)
T=${*:-cuphycontroller_scf dapp_tenant_main dapp_agent_test ru_emulator test_mac}
if [ "${FULL:-0}" = 1 ] || [ ! -f $BUILD_DIR/build.ninja ]; then
  TC=$SDK/cuPHY/cmake/toolchains/$TOOLCHAIN
  [ -f $TC ] || { echo "!! no toolchain file cuPHY/cmake/toolchains/$TOOLCHAIN (.env L1_TOOLCHAIN; available: $(ls $SDK/cuPHY/cmake/toolchains | tr '\n' ' '))"; exit 1; }
  echo "== configuring $BUILD_DIR (toolchain $TOOLCHAIN)"
  cmake -S $SDK -B $SDK/$BUILD_DIR -G Ninja -DCMAKE_TOOLCHAIN_FILE:FILEPATH=$TC -DCMAKE_BUILD_TYPE=Release -DSCF_FAPI_10_04=OFF -DENABLE_CONFORMANCE_TM_PDSCH_PDCCH=OFF -DSCF_FAPI_10_04_SRS=ON -DENABLE_DAPP_HOOK=ON -DBUILD_DOCS=OFF -DENABLE_PYAERIAL=OFF -DENABLE_TESTS=OFF
fi
echo "== ninja -C $BUILD_DIR -j ${JOBS:-$(nproc)} $T"
nice -n 10 ninja -C $SDK/$BUILD_DIR -j ${JOBS:-$(nproc)} $T
grep -E "^(CMAKE_TOOLCHAIN_FILE|CMAKE_BUILD_TYPE|ENABLE_DAPP_HOOK|SCF_FAPI_10_04|ENABLE_CONFORMANCE_TM_PDSCH_PDCCH|SCF_FAPI_10_04_SRS|ENABLE_PYAERIAL|ENABLE_TESTS):" $SDK/$BUILD_DIR/CMakeCache.txt
ls -la $SDK/$BUILD_DIR/cuPHY-CP/cuphycontroller/examples/cuphycontroller_scf $SDK/$BUILD_DIR/cuPHY-CP/dapp_hook/dapp_tenant_main
echo "== build done"
