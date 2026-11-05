"""PPO4R bridge: 18 radio features plus host NVML GPU utilization; no E2E input."""
import argparse
import json
import math
import os
from pathlib import Path
import socket
import signal
import time
from runtime import encode


def parse_request(data):
    words = data.decode('ascii').split()
    if len(words) != 22 or words[0] != 'PPO4R':
        raise ValueError('Expected PPO4R, four UE radio/PRB quadruples and two quotas')
    seq = int(words[1])
    if not 0 < seq < 2**64: raise ValueError('Bad sequence')
    values = [float(v) for v in words[2:20]]
    if not all(math.isfinite(v) for v in values): raise ValueError('Non-finite telemetry')
    ues = {}
    for i, name in enumerate(('yolo_1','yolo_2','live_1','live_2')):
        ues[name] = dict(zip(('ul_buffer_bytes','ul_snr_db','ul_tp_mbps','ul_prb_avg'),values[2+4*i:6+4*i]))
    return seq, dict(timestamp_ms=values[0],window_ms=values[1],ues=ues,
                     service_prbs=dict(zip(('yolo','live'),map(int,words[20:22]))))



def parse_fixed_gpu(value):
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise ValueError('PPO_GPU_INPUT_FIXED must be a finite percentage in 0..100')
    return result


def response(data, policy, minimum, now_ms=None, trace=None, gpu_reader=None,
             fixed_gpu=None):
    seq, sample = parse_request(data)
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if gpu_reader is None: raise ValueError('GPU reader required')
    gpu = gpu_reader.snapshot()
    fixed_gpu = parse_fixed_gpu(fixed_gpu)
    sample['gpu_util_percent'] = gpu['util_percent'] if fixed_gpu is None else fixed_gpu
    start = time.perf_counter_ns()
    # Actual source window is preserved, NOT relabelled as exactly 100ms.
    # This deployment explicitly accepts 80..120ms to accommodate E2 jitter.
    decision = policy.predict(sample, now_ms, window_tolerance_ms=20.)
    q = decision['service_prbs']
    a, b = q['yolo'], q['live']
    if type(a) is not int or type(b) is not int or a + b != 273 or min(a, b) < minimum:
        raise ValueError('Model output below MAC minimum or invalid total; not clamped')
    us = (time.perf_counter_ns() - start) // 1000
    if trace:
        trace.write(json.dumps(dict(wall_ms=now_ms,seq=seq,input=sample,
                                    encoded=encode(sample,273,minimum,now_ms,window_tolerance_ms=20.).tolist(),
                                    proposed_prbs=q,gpu=gpu,bridge_us=us,
                                    gpu_input_mode='measured' if fixed_gpu is None else 'fixed',
                                    gpu_util_measured=gpu['util_percent'],
                                    gpu_util_model_input=sample['gpu_util_percent']),allow_nan=False)+'\n')
        trace.flush()
    return f'{seq} OK {a} {b} {us}\n'.encode('ascii')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default=f'/tmp/oai-ppo-{os.getpid()}.sock')
    parser.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--min-prbs', type=int, default=5)
    args = parser.parse_args()
    try:
        fixed_gpu = parse_fixed_gpu(os.environ.get('PPO_GPU_INPUT_FIXED'))
    except ValueError as exc:
        parser.error(str(exc))
    if os.environ.get('PPO_RADIO_INPUT') != '1':
        parser.error('This radio-only bundle requires PPO_RADIO_INPUT=1 and a rebuilt xApp (PPO4R).')
    if not 1 <= args.min_prbs <= 136:
        parser.error('--min-prbs must be 1..136 and match the xApp/MAC minimum')
    # Import lazily: protocol tests do not require PyTorch.
    from runtime import BasicPolicy
    policy = BasicPolicy(args.bundle, num_threads=1)
    path = Path(args.socket)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    bound = False
    logdir = Path(__file__).resolve().parent.parent/'log'/'ppo_input_log'
    logdir.mkdir(parents=True, exist_ok=True)
    logpath=logdir/f'ppo_radio_gpu_inputs_{time.time_ns()}_{os.getpid()}.log'
    trace=logpath.open('x',encoding='utf-8')
    stopping = False
    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True
    previous_handlers = {sig: signal.signal(sig, request_stop)
                         for sig in (signal.SIGINT, signal.SIGTERM)}
    gpu_reader = None
    try:
        from gpu_reader import GpuReader
        gpu_reader = GpuReader()
        # Refuse an existing path. Never unlink another active server's socket.
        sock.bind(str(path))
        bound = True
        path.chmod(0o600)
        sock.settimeout(.2)
        print(f'[PPO] Radio input log: {logpath}',flush=True)
        print(f'[PPO] GPU input mode: {"measured" if fixed_gpu is None else "fixed=" + str(fixed_gpu)}; '
              'actual GPU telemetry remains recorded.', flush=True)
        print(f'PPO ready: CPU, UEs=4, inputs=19, total=273, socket={path}, '
              f'minimum={args.min_prbs}, window=80..120ms', flush=True)
        while not stopping:
            try:
                data, peer = sock.recvfrom(4096)
            except socket.timeout:
                continue
            seq = 0
            try:
                seq, _ = parse_request(data)
                reply = response(data, policy, args.min_prbs,trace=trace,gpu_reader=gpu_reader,
                                 fixed_gpu=fixed_gpu)
            except (ValueError, KeyError, TypeError, UnicodeError, OverflowError) as exc:
                print(f'HOLD seq={seq}: {exc}', flush=True)
                trace.write(json.dumps(dict(wall_ms=time.time()*1000,seq=seq,HOLD=str(exc)))+'\n')
                trace.flush()
                reply = f'{seq} ERR 0 0 0\n'.encode('ascii')
            try:
                sock.sendto(reply, peer)
            except (OSError, TypeError):
                pass  # client timed out/exited; never resend an old decision
    except KeyboardInterrupt:
        pass
    finally:
        if gpu_reader is not None: gpu_reader.close()
        sock.close()
        try:
            trace.close()
        except OSError as exc:
            print(f'[PPO] Input log close failed: {logpath}: {exc}', flush=True)
        else:
            print(f'[PPO] Input log saved: {logpath}', flush=True)
        finally:
            if bound:
                path.unlink(missing_ok=True)
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


if __name__ == '__main__':
    main()

