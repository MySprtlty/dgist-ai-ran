#!/bin/bash
# update_patches.sh [aerial-tree] - regenerate this directory from the Aerial work tree (default ../aerial-cuda-accelerated-ran):
#   local.patch  = text changes since BASE_COMMIT (committed or not), binary and untracked (non-ignored) files -> files/ with their paths.
#   testVectors/cuPhyChEstCoeffs.h5 (git-lfs, rewritten at run time) is never included. Run after changing Aerial sources, then commit aerial/.
#   The inverse is apply_patches.sh. BASE_COMMIT must exist on the public fork (git ls-remote origin hybrid) - it is what the submodule points at.
set -eu
S=$(cd "$(dirname "$0")" && pwd); T=${1:-$(dirname $S)/aerial-cuda-accelerated-ran}; BASE=$(cat $S/BASE_COMMIT)
git -C "$T" cat-file -e "$BASE^{commit}" || { echo "!! base $BASE not in $T"; exit 1; }
EXC=(':(exclude)testVectors/cuPhyChEstCoeffs.h5')
# binary files changed since the base (git diff --numstat prints "-" for them) + untracked files -> files/
rm -rf "$S/files"; mkdir -p "$S/files"; : > "$S/binary_files.txt"
git -C "$T" diff --numstat "$BASE" -- . "${EXC[@]}" | awk '$1=="-"{print $3}' > "$S/binary_files.txt"
git -C "$T" ls-files --others --exclude-standard >> "$S/binary_files.txt"
n=0; while read -r f; do [ -f "$T/$f" ] || continue; mkdir -p "$S/files/$(dirname "$f")"; cp -p "$T/$f" "$S/files/$f"; n=$((n+1)); done < "$S/binary_files.txt"
# text changes -> local.patch (binary paths excluded: they are in files/)
mapfile -t BIN < <(git -C "$T" diff --numstat "$BASE" -- . "${EXC[@]}" | awk '$1=="-"{print ":(exclude)"$3}')
git -C "$T" diff "$BASE" -- . "${EXC[@]}" "${BIN[@]}" > "$S/local.patch"
echo "aerial: base $(echo $BASE | cut -c1-12) | local.patch $(grep -c '^diff --git' $S/local.patch) files | files/ $n (binary + untracked)"
