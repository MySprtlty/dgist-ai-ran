#!/usr/bin/env python3
"""tgen_load.py show | init <level> | <level>      level = low | mid | high | auto | block number 1..255      (2026-11-13)

Run-time load selection of the 5-cell traffic generator (RU emulator + testMAC inside the dgist-l1 container; ./tgen.sh load <level>
calls this through docker exec). The launch pattern (.env TRAFFIC_GEN_PATTERN, default tmp_fluct) holds one load level per frame; both
tools replay the frame ("block") named by the 8-byte control word in --file, which they map read-only (RU_LP_BLOCK_CTL /
TESTMAC_LP_BLOCK_CTL): bits 63..56 = selection from extended frame F on, bits 55..48 = selection before F, bits 47..0 = F;
selection 0 = "auto" (the blocks alternate every TRAFFIC_GEN_BLOCK_REPEAT frames), k >= 1 = block k - 1 held. A change is written
--ahead frames (10 ms each) before it applies, so both tools switch at the same frame.
  init <level>  create the file / set the level with F = 0 (before the tools start - the entrypoint does this)
  <level>       change the level of a running generator
  show          print the current word:  load=<name> sel=<n> pending=<0|1>
Level names come from --levels (.env TRAFFIC_LEVELS, default low=1,mid=2,high=3 for tmp_fluct = 1 | 2 | 4 | 3 PUSCH UEs per cell and U slot)."""
import argparse
import ctypes
import mmap
import os
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("cmd"); ap.add_argument("level", nargs="?")
ap.add_argument("--file", default=os.environ.get("TRAFFIC_LOAD_FILE", "/dev/shm/dgist_tgen_load"))
ap.add_argument("--levels", default=os.environ.get("TRAFFIC_LEVELS") or "low=1,mid=2,high=3")
ap.add_argument("--ahead", type=int, default=30)
a = ap.parse_args()
names = {k.strip(): int(v) for k, v in (x.split("=") for x in a.levels.replace(" ", ",").split(",") if "=" in x)}
names["auto"] = 0
MASK = (1 << 48) - 1


def frame_now():
    return (time.time_ns() - 3640000000) // 10000000


def sel_of(s):
    if s in names: return names[s]
    if s.isdigit() and 0 <= int(s) <= 255: return int(s)
    sys.exit("unknown level %r (use %s or a block number)" % (s, " | ".join(sorted(names, key=names.get))))


def name_of(n):
    return next((k for k, v in names.items() if v == n), "block%d" % n)


def word(m):
    return ctypes.c_uint64.from_buffer(m)


if a.cmd == "init":
    sel = sel_of(a.level or "auto")
    fd = os.open(a.file, os.O_RDWR | os.O_CREAT, 0o644)
    if os.fstat(fd).st_size < 8: os.pwrite(fd, bytes(8), 0)
    m = mmap.mmap(fd, 8); w = word(m); w.value = (sel << 56) | (sel << 48); del w; m.close(); os.close(fd)
    print("load=%s sel=%d pending=0" % (name_of(sel), sel)); sys.exit(0)
if not os.path.exists(a.file) or os.path.getsize(a.file) < 8:
    sys.exit("no control word at %s (traffic generator not started with the load switch)" % a.file)
fd = os.open(a.file, os.O_RDONLY if a.cmd == "show" else os.O_RDWR)
m = mmap.mmap(fd, 8, prot=mmap.PROT_READ if a.cmd == "show" else mmap.PROT_READ | mmap.PROT_WRITE)
if a.cmd == "show":
    v = int.from_bytes(m[:8], "little"); f = v & MASK; pend = frame_now() < f
    print("load=%s sel=%d pending=%d%s" % (name_of(v >> 56), v >> 56, pend, " (now %s)" % name_of((v >> 48) & 255) if pend else "")); sys.exit(0)
sel = sel_of(a.cmd); w = word(m)
while frame_now() < (w.value & MASK): time.sleep(0.05)        # an earlier change has not applied yet: one change at a time
cur = w.value >> 56
if cur == sel: print("load=%s sel=%d pending=0 (unchanged)" % (name_of(sel), sel)); sys.exit(0)
w.value = (sel << 56) | (cur << 48) | ((frame_now() + a.ahead) & MASK)
print("load=%s sel=%d pending=1 (was %s, applies in %d ms)" % (name_of(sel), sel, name_of(cur), a.ahead * 10))
