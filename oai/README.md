# oai/ — the gNB source as patches (OAI + FlexRIC)

`./openairinterface5g` is not tracked in this repository (2.8 GB with history and build outputs). It is **upstream OAI + FlexRIC at pinned
commits plus the changes kept here**, recreated by `./oai/fetch_oai.sh` (≈1 minute of shallow fetches from gitlab.eurecom.fr).

| file | content |
|---|---|
| `oai_skipworktree.patch`, `oai_removed_files.txt` | state of the files the lab marked skip-worktree (ci-scripts run files, .dockerignore removed) |
| `oai_lab.patch` | hsw lab's OAI changes (MAC UL count-quota scheduler, E2 agent / slice SM, NGAP) + DGIST fixes (`docs/patches/oai_*.patch` are the DGIST hunks on their own) |
| `files/` + `oai_added_files.txt` | files added to the OAI tree (Dockerfile.gNB.aerial.ubuntu.dgist, lab scheduler variants, configs) |
| `flexric_lab.patch`, `flexric_files/` + `flexric_added_files.txt` | FlexRIC changes: xApps `xapp_slice_ppo_ctrl`, `xapp_ul_input_moni`, MAC SM IEs |
| `nvipc_src.2026.05.06.tar.gz` | nvIPC sources the OAI Aerial Dockerfile unpacks (from the Aerial 25-3 SDK; `cuPHY-CP/gt_common_libs/pack_nvipc.sh` regenerates one) |
| `fetch_oai.sh` | upstream clone + patches + files → `./openairinterface5g` |
| `update_patches.sh` | the inverse: regenerate this directory from the working tree after editing OAI/FlexRIC sources, then commit `oai/` |

Base commits: OAI `87c016b5842cb7e9952718fbac99f876a543d84a` (branch ARC1.7_integration, 2026-01-30 "Make L1 registry overridable"),
FlexRIC `ef6d722f22191eea74089966983da1f5ec1fedd4` (dev, 2026-05-27). Change descriptions: `docs/CHANGES_HSW_OAI.md`.
