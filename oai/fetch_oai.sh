#!/bin/bash
# fetch_oai.sh [target-dir] - recreate ./openairinterface5g: the hsw-lab OAI gNB (UL count-quota MAC scheduler + E2 agent + slice SM) with the
# DGIST fixes, from upstream sources plus the patches/files kept in this directory. Default target: <repo>/openairinterface5g (must not exist).
#   OAI     https://gitlab.eurecom.fr/oai/openairinterface5g.git  @ 87c016b5842cb7e9952718fbac99f876a543d84a  (2026-01-30, Aerial 25-3 integration)
#   FlexRIC https://gitlab.eurecom.fr/mosaic5g/flexric.git       @ ef6d722f22191eea74089966983da1f5ec1fedd4  (dev 2026-05-27) -> openair2/E2AP/flexric
#   + oai_lab.patch / files/ (OAI changes and added files), oai_skipworktree.patch / oai_removed_files.txt (files the lab marked skip-worktree), flexric_lab.patch / flexric_files/ (xApps, MAC SM IEs), nvipc_src.*.tar.gz
#     (nvIPC sources the OAI Aerial Dockerfile unpacks; regenerate with aerial-cuda-accelerated-ran/cuPHY-CP/gt_common_libs/pack_nvipc.sh)
# Next: ./dgist.sh build-oai (SETUP.md).  After editing OAI/FlexRIC sources: ./oai/update_patches.sh, commit oai/.
set -eu
S=$(cd "$(dirname "$0")" && pwd); REPO=$(dirname $S); T=${1:-$REPO/openairinterface5g}
OAI_URL=https://gitlab.eurecom.fr/oai/openairinterface5g.git; OAI_SHA=87c016b5842cb7e9952718fbac99f876a543d84a
FR_URL=https://gitlab.eurecom.fr/mosaic5g/flexric.git;      FR_SHA=ef6d722f22191eea74089966983da1f5ec1fedd4
[ -e "$T" ] && { echo "!! $T exists - move it away first"; exit 1; }
fetch() {  # <dir> <url> <sha>: shallow fetch of one commit (GitLab allows fetching by SHA); full fetch as the fallback
  mkdir -p "$1"; git -C "$1" init -q; git -C "$1" remote add origin "$2"
  git -C "$1" fetch -q --depth 1 origin "$3" 2>/dev/null || git -C "$1" fetch -q origin
  git -C "$1" checkout -q "$3"; git -C "$1" log -1 --format="   %h %cd %s" --date=short
}
echo "== OAI -> $T"; fetch "$T" $OAI_URL $OAI_SHA
echo "== FlexRIC -> $T/openair2/E2AP/flexric"; rm -rf "$T/openair2/E2AP/flexric"; fetch "$T/openair2/E2AP/flexric" $FR_URL $FR_SHA
echo "== lab + DGIST changes"
git -C "$T" apply --whitespace=nowarn "$S/oai_lab.patch"; echo "   oai_lab.patch: $(grep -c '^diff --git' $S/oai_lab.patch) files"
git -C "$T/openair2/E2AP/flexric" apply --whitespace=nowarn "$S/flexric_lab.patch"; echo "   flexric_lab.patch: $(grep -c '^diff --git' $S/flexric_lab.patch) files"
n=0; while read -r f; do mkdir -p "$T/$(dirname "$f")"; cp -p "$S/files/$f" "$T/$f"; n=$((n+1)); done < "$S/oai_added_files.txt"; echo "   $n added OAI files"
n=0; while read -r f; do mkdir -p "$T/openair2/E2AP/flexric/$(dirname "$f")"; cp -p "$S/flexric_files/$f" "$T/openair2/E2AP/flexric/$f"; n=$((n+1)); done < "$S/flexric_added_files.txt"; echo "   $n added FlexRIC files"
[ -s "$S/oai_skipworktree.patch" ] && { git -C "$T" apply --whitespace=nowarn "$S/oai_skipworktree.patch"; echo "   oai_skipworktree.patch: $(grep -c '^+++ ' $S/oai_skipworktree.patch) files (the lab's skip-worktree files)"; }
[ -s "$S/oai_removed_files.txt" ] && while read -r f; do rm -f "$T/$f"; done < "$S/oai_removed_files.txt" && echo "   $(grep -c . $S/oai_removed_files.txt) files removed (as in the lab tree)"
cp -p "$S"/nvipc_src.*.tar.gz "$T/" && echo "   $(ls "$S"/nvipc_src.*.tar.gz | xargs -n1 basename) copied (docker/Dockerfile.gNB.aerial.ubuntu.dgist unpacks it)"
echo "== done: $T"; echo "   $(git -C "$T" status --porcelain | grep -c .) changed/added paths vs upstream | next: ./dgist.sh build-oai"
