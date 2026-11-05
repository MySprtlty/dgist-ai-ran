"""19-input CPU actor: radio18 + GPU utilization. NVML is owned by the bridge."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time
import torch

SCHEMA = 'ppo19-ul-4ue-273-radio-prb-gpu-v1'
UE_IDS = ('yolo_1', 'yolo_2', 'live_1', 'live_2')
UE_SERVICES = dict(zip(UE_IDS, ('yolo', 'yolo', 'live', 'live')))
SERVICES = ('yolo', 'live')
FEATURES = [f'{ue}.{key}' for ue in UE_IDS
            for key in ('ul_buffer_bytes', 'ul_snr_db', 'ul_tp_mbps', 'ul_prb_avg')] + [
                'service_prbs.yolo', 'service_prbs.live', 'gpu_util_percent']
SCALES = {'ul_buffer_bytes': 250000., 'ul_snr_db': 20., 'ul_tp_mbps': 20., 'ul_prb_avg': 100.}


class BasicActor(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.shared = torch.nn.Sequential(torch.nn.Linear(len(FEATURES), 128), torch.nn.ReLU(),
                                          torch.nn.Linear(128, 128), torch.nn.ReLU())
        self.mean = torch.nn.Linear(128, 1)

    def forward(self, state):
        return torch.sigmoid(self.mean(self.shared(state)).squeeze(-1))


def number(value, name, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    if minimum is not None and value < minimum:
        raise ValueError(f'{name} must be >= {minimum}')
    return float(value)


def encode(sample, total_prbs, min_prbs=1, now_ms=None, max_age_ms=200., window_tolerance_ms=0.):
    """Timestamp and measurement window are validated metadata, not NN features."""
    if set(sample) != {'timestamp_ms', 'window_ms', 'ues', 'service_prbs', 'gpu_util_percent'}:
        raise ValueError('Unexpected/missing top-level telemetry fields')
    stamp = number(sample['timestamp_ms'], 'timestamp_ms', 0)
    tolerance = number(window_tolerance_ms, 'window_tolerance_ms', 0)
    if tolerance > 20:
        raise ValueError('Window tolerance cannot exceed 20ms')
    if abs(number(sample['window_ms'], 'window_ms', 0) - 100.) > tolerance:
        raise ValueError('TP measurement window differs from 100ms beyond allowed tolerance')
    if now_ms is not None:
        age = number(now_ms, 'now_ms', 0)-stamp
        if age < -5 or age > max_age_ms:
            raise ValueError('Future/stale telemetry; do not send a new control command')
    if set(sample['ues']) != set(UE_IDS):
        raise ValueError('Expected two mapped YOLO UEs and two live UEs')
    values = []
    for ue in UE_IDS:
        row = sample['ues'][ue]
        if set(row) != set(SCALES):
            raise ValueError(f'Unexpected/missing fields for {ue}')
        for key, scale in SCALES.items():
            v = number(row[key], f'{ue}.{key}', None if key == 'ul_snr_db' else 0)
            if key == 'ul_snr_db' and not -50 <= v <= 80:
                raise ValueError('SNR must be in dB, within the configured input range [-50, 80]')
            if key == 'ul_prb_avg' and v > total_prbs + 1:
                raise ValueError('PRB average exceeds cell capacity; check TDD/counters')
            values.append(v/(scale+abs(v)))
    budgets = sample['service_prbs']
    if set(budgets) != set(SERVICES):
        raise ValueError('Expected yolo/live PRB budgets')
    if any(type(budgets[s]) is not int or budgets[s] < min_prbs for s in SERVICES):
        raise ValueError('Current applied budgets must be positive integer PRB counts')
    if sum(budgets.values()) != total_prbs:
        raise ValueError('PRB total differs from trained model; use a compatible model')
    values.extend(budgets[s]/total_prbs for s in SERVICES)
    gpu = number(sample['gpu_util_percent'], 'gpu_util_percent', 0)
    if gpu > 100: raise ValueError('GPU utilization exceeds 100%')
    values.append((gpu - 69.2) / 10.)
    return torch.tensor(values, dtype=torch.float32)


def counts_from_fraction(fraction, total_prbs, min_prbs):
    fraction = number(fraction, 'actor output', 0)
    if fraction > 1:
        raise ValueError('Actor output exceeds 1')
    yolo = max(min_prbs, min(total_prbs-min_prbs, math.floor(total_prbs*fraction+.5)))
    return {'yolo': yolo, 'live': total_prbs-yolo}


class BasicPolicy:
    def __init__(self, bundle=None, num_threads=1):
        self.bundle = Path(bundle) if bundle else Path(__file__).resolve().parent
        self.manifest = json.loads((self.bundle/'manifest.json').read_text())
        m = self.manifest
        if m.get('gpu_normalization') != '(percent - 69.2) / 10':
            raise ValueError('Incompatible GPU normalization')
        if m.get('total_ul_prbs') != 273 or m.get('simulation_config', {}).get('borrow_unused_prbs') is not False:
            raise ValueError('This deployment requires a 273-PRB, no-lending trained model')
        if m['schema'] != SCHEMA or m['features'] != FEATURES or m['normalization'] != SCALES:
            raise ValueError('Incompatible model input contract')
        if m['control_interval_ms'] != 100 or m['ue_services'] != UE_SERVICES:
            raise ValueError('Unsupported timing or UE/service mapping')
        self.total = m['total_ul_prbs']
        self.minimum = m['min_slice_prbs']
        if type(self.total) is not int or type(self.minimum) is not int or not 1 <= self.minimum <= self.total//2:
            raise ValueError('Invalid model PRB bounds')
        path = self.bundle/'actor.pt'
        if hashlib.sha256(path.read_bytes()).hexdigest() != m['actor_sha256']:
            raise ValueError('Actor checksum mismatch')
        torch.set_num_threads(num_threads)
        self.actor = BasicActor()
        self.actor.load_state_dict(torch.load(path, map_location='cpu', weights_only=True), strict=True)
        self.actor.eval()
        with torch.inference_mode():
            self.actor(torch.zeros(len(FEATURES)))

    def predict(self, sample, now_ms, *, window_tolerance_ms=0.):
        state = encode(sample, self.total, self.minimum, now_ms,
                       window_tolerance_ms=window_tolerance_ms)
        with torch.inference_mode():
            fraction = float(self.actor(state).item())
        counts = counts_from_fraction(fraction, self.total, self.minimum)
        return dict(schema=SCHEMA, telemetry_timestamp_ms=sample['timestamp_ms'],
                    control_interval_ms=100, total_ul_prbs=self.total,
                    borrow_unused_prbs=False,
                    service_prbs=counts,
                    service_ratios={s: counts[s]/self.total for s in SERVICES},
                    service_percentages={s: 100*counts[s]/self.total for s in SERVICES})


class XAppController:
    """Call from the xApp's 100-ms timer. send_control must implement real E2 control.

    Failures propagate: the host must alarm/retain a confirmed safe allocation.
    Predictions are never assumed to be the next measured/applied PRB budgets.
    """
    def __init__(self, policy, send_control):
        self.policy, self.send_control = policy, send_control
        self.last_ms = None
        self.last_sample_ms = None

    def tick(self, sample, now_ms):
        now_ms = number(now_ms, 'now_ms', 0)
        if self.last_ms is not None and now_ms-self.last_ms < 100:
            return None
        if self.last_sample_ms is not None and sample['timestamp_ms'] <= self.last_sample_ms:
            raise ValueError('Duplicate/out-of-order telemetry')
        decision = self.policy.predict(sample, now_ms)
        # Rate-limit attempts even if the transport fails; acknowledgement is host-owned.
        self.last_ms, self.last_sample_ms = now_ms, sample['timestamp_ms']
        self.send_control(decision)
        return decision


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Offline inference smoke test; sends NO E2 command')
    p.add_argument('--input', type=Path, default=Path(__file__).with_name('sample_input.json'))
    p.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent)
    p.add_argument('--offline', action='store_true', help='Use sample timestamp as clock for saved-file replay only')
    args = p.parse_args()
    sample = json.loads(args.input.read_text())
    print(json.dumps(BasicPolicy(args.bundle).predict(sample,
        sample['timestamp_ms'] if args.offline else time.time()*1000), indent=2))
