"""Offline contract and controller checks; no E2 commands are transmitted."""
import copy
import json
from pathlib import Path
import time

from runtime import BasicPolicy, XAppController


def main():
    root = Path(__file__).resolve().parent
    policy = BasicPolicy(root)
    sample = json.loads((root / 'sample_input.json').read_text())
    now = sample['timestamp_ms']
    decision = policy.predict(sample, now)
    assert sum(decision['service_prbs'].values()) == 78
    assert all(1 <= n <= 77 for n in decision['service_prbs'].values())
    assert decision['borrow_unused_prbs'] is False
    jitter = copy.deepcopy(sample)
    jitter['window_ms'] = 110.
    try:
        policy.predict(jitter, now)
    except ValueError:
        pass
    else:
        raise AssertionError('Default contract must remain exactly 100ms')
    policy.predict(jitter, now, window_tolerance_ms=20.)
    jitter['window_ms'] = 121.
    try:
        policy.predict(jitter, now, window_tolerance_ms=20.)
    except ValueError:
        pass
    else:
        raise AssertionError('Reject windows outside explicitly allowed jitter')
    for change in ('stale', 'nan', 'missing', 'budget'):
        bad = copy.deepcopy(sample)
        if change == 'stale':
            bad['timestamp_ms'] = now - 201
        elif change == 'nan':
            bad['ues']['yolo_1']['ul_snr_db'] = float('nan')
        elif change == 'missing':
            del bad['ues']['live_1']['ul_tp_mbps']
        else:
            bad['service_prbs']['yolo'] += 1
        try:
            policy.predict(bad, now)
        except ValueError:
            pass
        else:
            raise AssertionError(change)
    sent = []
    controller = XAppController(policy, sent.append)
    assert controller.tick(sample, now) is not None
    assert controller.tick(sample, now + 99) is None
    following = copy.deepcopy(sample)
    following['timestamp_ms'] = now + 100
    assert controller.tick(following, now + 100) is not None
    assert len(sent) == 2
    timings = []
    for _ in range(1000):
        start = time.perf_counter()
        policy.predict(sample, now)
        timings.append((time.perf_counter() - start) * 1000)
    print(json.dumps(decision, indent=2))
    print('PASS: input validation, integer quotas, no-lending contract, 100-ms gate')
    print(f'Local warm predict: mean={sum(timings)/len(timings):.4f} ms; '
          f'p99={sorted(timings)[989]:.4f} ms (excludes E2)')


if __name__ == '__main__':
    main()
