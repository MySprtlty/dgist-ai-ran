# Strict-Quota PPO Inference Component

This is the separately trained NO-LENDING comparison model, not a replacement
for the existing project-root xapp_bundle (lending-enabled model).

Same 8 inputs, two UEs, total 78 PRBs, 100-ms decisions. Service quotas are
strict: neither UE may borrow the other service's unused PRBs. The DU must
implement these semantics. Unused quotas can remain idle even with another
service backlogged. The actor does not implement the slot scheduler.

Training: 1200 episodes, seed 7, --no-borrow; all other configuration, reward,
QoS thresholds and hyperparameters unchanged. Thresholds 150/200 ms are
provisional. Consult manifest.json and DEPLOYMENT.md for assumptions.

Offline test from this directory: `python runtime.py --offline`.
For E2 integration and metric units, see DEPLOYMENT.md.
Run `python verify.py` for offline validation and local inference timing.
No real RIC connection or on-target timing validation has been performed.
