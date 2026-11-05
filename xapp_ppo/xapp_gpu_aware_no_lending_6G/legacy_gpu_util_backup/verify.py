"""Offline inference contract check; no E2 request, no GPU hardware needed."""
import json
from pathlib import Path
from runtime import BasicPolicy
R=Path(__file__).resolve().parent;p=BasicPolicy(R);s=json.loads((R/'sample_input.json').read_text())
for g in [0,69.2,78.2,83.3,100]:
 s['gpu_util_percent']=g;d=p.predict(s,s['timestamp_ms']);q=d['service_prbs'];assert sum(q.values())==273 and min(q.values())>=5;print(g,q)
for g in [-1,101,float('nan')]:
 s['gpu_util_percent']=g
 try:p.predict(s,s['timestamp_ms']);raise AssertionError('Invalid GPU accepted')
 except ValueError:pass
print('PASS: radio19 input, checksum, GPU range and integer budgets. RF performance NOT verified.')
