#!/bin/bash
# scripts/fetch_testvectors.sh [archive] | verify | pack [out]   (run by the account that owns the package directory)
#   Installs the test vectors of the 5 emulator cells (traffic generator, ./tgen.sh) into .env TRAFFIC_GEN_TV_DIR (default ./tgen/testVectors).
#   The set is too big for git (187 files, 0.85 GB: 100 TVnr_DLMIX_* + 85 TVnr_ULMIX_* + cuPhyChEstCoeffs.h5 + the launch pattern
#   multi-cell/launch_pattern_F08_5C_tmp_fluct.yaml), so it travels as one archive: dgist_tgen_testvectors_20261113.tar.xz (354 MB).
#   [archive]  a local copy of the archive (also: TV_ARCHIVE=<file>); without it the archive is downloaded from TV_URL
#              (default: the release asset of this repository named below) into the directory next to TRAFFIC_GEN_TV_DIR
#   verify     only checks the installed files against config/tgen/testvectors.sha256 (one sha256 per file)
#   pack [out] writes the archive from an installed, verified set (to publish it or to carry it to another host)
#   Every install ends with the per-file check, whatever the source of the archive was.
set -u
[ "$(id -u)" = 0 ] && { echo "!! run this as the account that owns this directory (not root / sudo)"; exit 1; }
D=$(cd "$(dirname "$0")/.." && pwd); cd $D
set -a; . $D/.env; set +a; . $D/scripts/paths.sh
TV=$TRAFFIC_GEN_TV_DIR; LIST=$D/config/tgen/testvectors.sha256
NAME=dgist_tgen_testvectors_20261113.tar.xz; SHA=807031d0c6e1014d6f2704765966aadac939bef13ecdb7905135374a7c9861c5
URL=${TV_URL:-https://github.com/MySprtlty/dgist-ai-ran/releases/download/tgen-testvectors-20261113/$NAME}
verify() { [ -d $TV ] && ( cd $TV && sha256sum -c --quiet $LIST ); }
case "${1:-}" in
verify)
  verify && echo "test vectors OK: $(wc -l < $LIST) files in $TV" || { echo "!! $TV does not match config/tgen/testvectors.sha256 (run $0 to install the set)"; exit 1; } ;;
pack)
  out=${2:-$D/$NAME}
  verify > /dev/null 2>&1 || { echo "!! $TV is not a complete, verified set"; exit 1; }
  sed -E 's/^[0-9a-f]{64}  //' $LIST | tar --sort=name --owner=0 --group=0 --numeric-owner -C $TV -cf - -T - | xz -6 -T0 > $out || exit 1
  echo "wrote $out ($(du -h $out | cut -f1)), sha256 $(sha256sum $out | cut -d' ' -f1)" ;;
*)
  verify > /dev/null 2>&1 && { echo "test vectors already installed: $(wc -l < $LIST) files in $TV"; exit 0; }
  A=${1:-${TV_ARCHIVE:-}}; fetched=0
  if [ -z "$A" ]; then
    A=$(dirname $TV)/$NAME; mkdir -p $(dirname $TV) || exit 1
    echo "downloading $URL"
    if command -v curl > /dev/null; then curl -fL --retry 3 -o $A.part "$URL"; else wget -O $A.part "$URL"; fi \
      || { rm -f $A.part; echo "!! download failed. Get $NAME by hand and run: $0 <archive>   (or set TV_URL)"; exit 1; }
    echo "$SHA  $A.part" | sha256sum -c --quiet || { echo "!! the downloaded archive has a wrong sha256 (kept as $A.part)"; exit 1; }
    mv $A.part $A; fetched=1
  else
    [ -f "$A" ] || { echo "!! no such archive: $A"; exit 1; }
    echo "$SHA  $A" | sha256sum -c --quiet 2> /dev/null || echo "note: $A is not the published archive (other sha256); the per-file check decides"
  fi
  mkdir -p $TV && tar -xJf "$A" -C $TV || { echo "!! extracting $A into $TV failed"; exit 1; }
  verify || { echo "!! $TV does not match config/tgen/testvectors.sha256 after extraction"; exit 1; }
  [ $fetched = 1 ] && rm -f $A
  echo "test vectors installed: $(wc -l < $LIST) files, $(du -sh $TV | cut -f1) in $TV" ;;
esac
