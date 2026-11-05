#!/bin/bash
# update_patches.sh [oai-tree] - regenerate this directory (oai_lab.patch, flexric_lab.patch, files/, flexric_files/, *_added_files.txt) from the
# working tree ./openairinterface5g: run after changing OAI or FlexRIC sources, then commit this directory. Build outputs, logs and the
# nvipc sources are left out. The inverse is fetch_oai.sh (upstream clone + these changes).
set -eu
S=$(cd "$(dirname "$0")" && pwd); T=${1:-$(dirname $S)/openairinterface5g}; F=$T/openair2/E2AP/flexric
git -C $T diff -- . ':(exclude)openair2/E2AP/flexric' > $S/oai_lab.patch
git -C $F diff > $S/flexric_lab.patch
rm -rf $S/files $S/flexric_files; mkdir -p $S/files $S/flexric_files
git -C $T status --porcelain --untracked-files=all | grep '^??' | awk '{print $2}' | grep -v -E '^nvipc_src|(^|/)log(/|$)|\.log$|^cmake_targets/|\.pcap$' > $S/oai_added_files.txt
while read -r f; do mkdir -p "$S/files/$(dirname "$f")"; cp -p "$T/$f" "$S/files/$f"; done < $S/oai_added_files.txt
git -C $F status --porcelain --untracked-files=all | grep '^??' | awk '{print $2}' | grep -v -E '^build' > $S/flexric_added_files.txt
while read -r f; do mkdir -p "$S/flexric_files/$(dirname "$f")"; cp -p "$F/$f" "$S/flexric_files/$f"; done < $S/flexric_added_files.txt
# files the lab marked skip-worktree (git ls-files -v -> "S"): `git diff` skips them, so their state is captured separately
: > $S/oai_skipworktree.patch; : > $S/oai_removed_files.txt
git -C $T ls-files -v | awk '/^S /{print $2}' | while read -r f; do
  if [ -e "$T/$f" ]; then git -C $T show "HEAD:$f" | diff -u --label "a/$f" --label "b/$f" - "$T/$f" >> $S/oai_skipworktree.patch || true
  else echo "$f" >> $S/oai_removed_files.txt; fi
done
echo "skip-worktree: $(grep -c '^+++ ' $S/oai_skipworktree.patch) files patched, $(grep -c . $S/oai_removed_files.txt) removed"
echo "oai: $(grep -c '^diff --git' $S/oai_lab.patch) files patched, $(wc -l < $S/oai_added_files.txt) added | flexric: $(grep -c '^diff --git' $S/flexric_lab.patch) files patched, $(wc -l < $S/flexric_added_files.txt) added"
