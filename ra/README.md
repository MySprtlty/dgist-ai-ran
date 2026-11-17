# ra/ — Resource Allocator (ICNL, IITP 6G demo)

The Resource Allocator (RA) decides two things per VA frame: how many UL PRBs per slot the phone's upload is guaranteed (a floor), and
which YOLO model its RUN uses. The goal is the highest accuracy (mAP) whose result is still back at the phone within the deadline
(`.env VA_DEADLINE_MS`, 200 ms). It works inside the VA quota of the slicing budget and leaves the non-VA UEs to their quota.

```
budget: .env RA_STATIC_BUDGET, or the PPO slicing xApp in RA mode ──VA quota──▶ RA ──quotas, UE association, UL PRB floor per UE (E2)──▶ gNB MAC
dApp agent (FAPI-aware scheduler) ──server time per model──▶ RA ──model + server deadline per frame──▶ VA service ──RUN──▶ dApp
gNB MAC (E2, 10 ms) ──MCS, BLER, bytes sent per UE──▶ RA ◀──frame start / upload progress / size / phone IP── VA service (VA_RA_HOOK)
```

## Files

| file | what |
|---|---|
| `ra_main.py` | the RA process: local UDP I/O, the 10 ms loop, logs (`ra_main.py --help`) |
| `ra_policy.py` | the decisions, no I/O: `plan` (every 10 ms) and `decide` (per frame) |
| `xapp/xapp_ccma_ra.c`, `xapp/CMakeLists.txt` | E2 side, a FlexRIC xApp built against this package's FlexRIC (`../ra.sh build`): MAC + Slice SM reports every 10 ms to the RA as JSON; RA commands to Slice SM control (quota `NR_UL_PRB_COUNT_V1`, UE association, delete) and E2SM-RC Control Style 2 action 101 parameter 5 (UL PRB floor, `RA_RC_FLOORS=1`) |
| `ra_slicing_link/` | the RA ↔ slicing xApp interface v1 (`INTERFACE.md`, Korean): C header for the xApp side, Python module for the RA side, `ra_stub.py`, `test_link.sh` |
| `va_hook.py` | client of the RA hook (the VA service has its copy in `va_service/va_hook.py`) |
| `tools/fake_dapp_agent.py`, `tools/va_sim.py` | test only: a dApp agent with a modelled GPU, phones + VA service + the UL PRB split in real time |
| `test/run_ra_test.sh`, `test/emu/` | test only: the whole RA against FlexRIC's emulated gNB (see the script header) |

The gNB side is in `../oai/oai_lab.patch` (2026-11-17; on its own: `../docs/patches/oai_ra_ul_prb_floor.patch`, description:
`../docs/CHANGES_HSW_OAI.md`). Without floors from the RA the gNB behaves as before.

## Inputs

- dApp agent (TCP `DAPP_AGENT_PORT`, role `ra`, `estimate` every 20 slots): server time (queue + decode + inference) of each YOLO model
  with the SMs the AI has now. Without the agent: a static table (`ra_policy.STATIC_MS`, 80 SM 4K).
- Budget: VA and non-VA PRBs per UL slot (sum 273), static (`.env RA_STATIC_BUDGET`) or from the slicing xApp (`budget`, `ra_slicing_link/INTERFACE.md`).
- gNB MAC (MAC SM, 10 ms): per UE MCS, BLER, BSR, bytes and PRBs sent → bits one PRB carries per slot.
- VA service (UDP hook, `va_service/ra_link.py`): per frame the phone's IP, frame id, send time (`X-T0-Ms`), size and upload progress;
  `decide` when the whole frame is in, `done` after the RUN. The RA finds the UE's RNTI from the phone's IP (`ra.sh start` → `--ue-ip`).

## Decisions (`ra_policy.py`)

- `plan`, every 10 ms, for the frames still uploading: every combination of a model per UE and a GPU order (all orders up to 3 UEs, else
  earliest deadline first). For each, backwards from the deadline: minus 5 ms result downlink, minus 5 ms margin, minus the model's server
  time = the latest inference start; the bytes left must be up by then → PRBs per slot = the UE's floor. Among the candidates whose floors
  fit the VA quota: the highest mAP sum; ties keep the last plan (floors do not flip every 10 ms), then the fewest PRBs. Nothing fits:
  the lightest model for all, the quota to the earliest deadlines first.
- `decide`, when a frame is complete: time left = deadline − upload time − downlink; the model with the highest mAP whose server time
  (plus the GPU queue of the RA's own RUNs) fits the time left minus 3 ms. While other VA frames are uploading, not above the plan's model.
- mAP (COCO 50-95): 26n 40.9, 26s 48.6, 26m 53.1, 26l 55.0, 26x 57.5.

## Run (DGIST package)

1. gNB with the floor patch: a tree from `./oai/fetch_oai.sh` has it; an older `openairinterface5g/` gets it with
   `git -C openairinterface5g apply ../docs/patches/oai_ra_ul_prb_floor.patch`. Then `./dgist.sh build-oai`. (Without it: `.env RA_RC_FLOORS=0`.)
2. `./ra.sh build` (FlexRIC built by `./ric.sh build`).
3. `.env`: `VA_RA_HOOK=127.0.0.1:9822` (= `RA_HOOK`), `RA_BUDGET`, `RA_STATIC_BUDGET`. Not together with `./xapp.sh start`.
4. `./all.sh up` (or core → `./ric.sh start` → `./dgist.sh up`), phones attached, `./va.sh restart`, `./nonva.sh start`.
5. `./ra.sh start` (RNTIs from `.env VA_IMSIS / NONVA_IMSIS`, UE IPs from `ue_table.py --ue-ips`; or `./ra.sh start VA1,VA2 NV1,NV2 ip=rnti,...`).
6. `./ra.sh status`, `./ra.sh logs`; `./ra.sh stop` removes the quotas (Slice SM delete) and stops the E2 side. `./all.sh down` does it too.

PPO slicing xApp as the budget (`RA_BUDGET=ppo`): start the RA first, then the PPO xApp with `PPO_OUTPUT=ra` (`PPO_RA_ADDR=RA_SLICING_LISTEN`,
`PPO_RA_LISTEN=RA_PPO_ADDR`). That mode of `xapp_slice_ppo_ctrl` is the Slicing lab's change and is not in this repository yet.

Logs: `runs/ra/ra.log`, `runs/ra/e2.log`, and per run `runs/ra/<yymmdd_HHMMSS>/`: `ticks.csv` (every plan: VA quota, floors, models),
`frames.csv` (every frame: size, upload, model, deadline, predicted / measured server time, on time), `budgets.csv`, `cmds.csv` (E2 commands and acks).

## Tested

- ICNL GH200 (2026-11-14, DGIST stack at 1a7fb78 + this RA, 4 phones): static budget 164:109 — 2,697 / 2,697 RA decisions, 5,195 floor
  commands acked, `[RA_FLOOR]` grants in the gNB log; PPO xApp in RA mode — 369 budgets applied, VA 2,994 / 3,002 frames within 200 ms.
- This layout (2026-11-17): `./ra.sh build` against the FlexRIC `build_dgist` of the ICNL GH200 copy (aarch64, no warnings) and against
  an x86 FlexRIC `ef6d722f` + `flexric_lab.patch`; on x86 with FlexRIC's emulated gNB: `./ra.sh start` (quota 164:109 and association
  applied, RC floors sent), the VA simulator through the hook (100 frames, all decided, all within 200 ms), `./ra.sh stop` (quotas
  removed: the emulated gNB is back at 0:0); `ra_slicing_link/test_link.sh` 4 / 4 PASS; `test/run_ra_test.sh static ra`.
- Not yet: this branch with the real gNB and phones (the RA code is the one of the 2026-11-14 run plus the quota removal on exit and the
  package paths).
