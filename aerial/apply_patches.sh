#!/bin/bash
# apply_patches.sh [aerial-tree] - bring the Aerial submodule (checked out at BASE_COMMIT by `git clone --recursive`) to the DGIST state:
#   git apply local.patch (text changes) + copy files/ (binary and new files). Idempotent: an already-applied tree is detected and left alone.
#   Needs git-lfs installed before the clone (testVectors/*.h5 are LFS objects the L1 reads). Next: ./dgist.sh build-l1
set -eu
S=$(cd "$(dirname "$0")" && pwd); T=${1:-$(dirname $S)/aerial-cuda-accelerated-ran}; BASE=$(cat $S/BASE_COMMIT)
[ -f "$T/cuPHY-CP/dapp_hook/CMakeLists.txt" ] || { echo "!! $T is empty - git submodule update --init aerial-cuda-accelerated-ran"; exit 1; }
head=$(git -C "$T" rev-parse HEAD)
[ "$head" = "$BASE" ] || echo "   note: HEAD $(echo $head | cut -c1-12) != base $(echo $BASE | cut -c1-12) (patch was made against the base)"
if git -C "$T" apply --check --reverse "$S/local.patch" 2>/dev/null; then echo "   local.patch already applied"
else git -C "$T" apply --whitespace=nowarn "$S/local.patch" && echo "   local.patch applied: $(grep -c '^diff --git' $S/local.patch) files"; fi
n=0; (cd "$S/files" && find . -type f | sed 's#^\./##') | while read -r f; do mkdir -p "$T/$(dirname "$f")"; cp -p "$S/files/$f" "$T/$f"; done; echo "   $(find $S/files -type f | wc -l) files copied (binary + new)"
echo "== Aerial tree ready: $(git -C "$T" status --porcelain | grep -c .) changed paths vs $(echo $BASE | cut -c1-12) | next: ./dgist.sh build-l1"
