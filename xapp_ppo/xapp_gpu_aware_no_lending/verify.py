"""Offline model/bridge check: no GPU query and no E2 commands."""
import copy
import json
from pathlib import Path
import time
from runtime import BasicPolicy,encode
from ppo_socket_server import response

root=Path(__file__).resolve().parent
policy=BasicPolicy(root)
sample=json.loads((root/'sample_input.json').read_text())
sample['gpu_util_percent']=70.
assert len(encode(sample,273,5,sample['timestamp_ms']))==19
for util in (0.,55.,70.,85.,100.):
    sample['gpu_util_percent']=util
    result=policy.predict(sample,sample['timestamp_ms'])
    q=result['service_prbs']
    assert sum(q.values())==273 and min(q.values())>=5
    print('GPU',util,'PRBs',q)
for util in (-1,101,float('nan')):
    sample['gpu_util_percent']=util
    try:policy.predict(sample,sample['timestamp_ms'])
    except ValueError:pass
    else:raise AssertionError('Invalid GPU accepted')
sample['gpu_util_percent']=70.
sample['window_ms']=110.
policy.predict(sample,sample['timestamp_ms'],window_tolerance_ms=20.)
class Latency:
    def augment(self,s,now):
        for u in s['ues'].values():u['service_latency_ms']=-1.
        return {}
class GPU:
    def snapshot(self):return {'util_percent':70.,'memory_used_gib':45.}
reply=response(b'PPO4 1 1000 100 0 21 2 0 21 2 0 21 2 0 21 2 137 136',
               policy,5,1000,reader=Latency(),gpu_reader=GPU())
assert reply.startswith(b'1 OK ')
print('PASS: 19-input actor, GPU bounds, integer budgets, PPO4 bridge. RF/GPU hardware not tested.')
