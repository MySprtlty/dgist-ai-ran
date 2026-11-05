#!/bin/bash
# build_oai_image.sh - rebuild $OAI_IMAGE from ./openairinterface5g (hsw MAC scheduler / E2 agent changes + DGIST empty-UCI fix) with the
# docker/Dockerfile.gNB.aerial.ubuntu.dgist (= the tree's Dockerfile + --build-e2 for the E2 agent; stages: ran-base:latest -> ran-build -> oai-gnb-aerial). The legacy builder is used on
# purpose: its --cpuset-cpus keeps the ~10700-object compile on the housekeeping cores ($BUILD_CPUS) while a real-time stack may be running.
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); . $D/.env
cd $D/openairinterface5g
docker image inspect ran-base:latest > /dev/null 2>&1 || { echo "ran-base:latest missing - first: docker build -f docker/Dockerfile.base.ubuntu -t ran-base:latest ."; exit 1; }
mkdir -p $D/runs; LOG=$D/runs/build_oai_image_$(date +%m%d_%H%M).log
echo "== docker build -t $OAI_IMAGE (cores $BUILD_CPUS, log $LOG) $(date +%T)"
DOCKER_BUILDKIT=0 docker build --cpuset-cpus ${BUILD_CPUS:-0-3,65-71} -f docker/Dockerfile.gNB.aerial.ubuntu.dgist -t $OAI_IMAGE . > $LOG 2>&1 && echo "== image $OAI_IMAGE built $(date +%T)" || { echo "!! build failed - tail $LOG"; tail -20 $LOG; exit 1; }
docker image inspect $OAI_IMAGE --format '   {{.Id}} {{.Created}}'
