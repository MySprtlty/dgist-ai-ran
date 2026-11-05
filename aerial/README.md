# aerial/ — DGIST changes to the Aerial tree, as a patch on the public fork commit

`aerial-cuda-accelerated-ran/` is a submodule of the fork **MySprtlty/aerial-cuda-accelerated-ran**, pinned at `BASE_COMMIT` (branch `hybrid`,
the last commit that is on GitHub). Everything DGIST changed after that commit is **not pushed to the fork**; it lives here and is applied once
after the clone:

```bash
git lfs install                                   # testVectors/*.h5 are LFS objects
git clone --recursive https://github.com/MySprtlty/dgist-ai-ran.git /home/nvidia/_dgist
cd /home/nvidia/_dgist && ./aerial/apply_patches.sh
```

| file | content |
|---|---|
| `BASE_COMMIT` | the fork commit the submodule points at (`git ls-remote origin hybrid` on the fork) |
| `local.patch` | text changes since the base: `cuPHY-CP/dapp_hook/` (tenant `sm_report.last`, sizer, cots/ package, xapp_flexric/), DGIST L1 configs, TV yamls, CONTEXT.md |
| `files/` + `binary_files.txt` | binary and new files: test frames (`models/img4k`, `models/imgfhd`), briefing decks, `sweep/fullstack/fhd_bench.sh` |
| `apply_patches.sh` / `update_patches.sh` | apply after clone / regenerate after editing Aerial sources (then commit `aerial/`) |

Base of the fork: NVIDIA Aerial CUDA-Accelerated RAN 25-3 (`654c249`). Change descriptions: `docs/CHANGES_DGIST_AERIAL.md`, `docs/patches/aerial_25-3_dgist_nvidia_files.patch`.
