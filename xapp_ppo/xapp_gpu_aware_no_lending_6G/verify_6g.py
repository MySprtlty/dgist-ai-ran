"""Offline CPU/bridge contract test. Does not send an E2 control command."""
import copy,json,time,tempfile
from pathlib import Path
import torch
from sm_runtime import BasicPolicy,encode,MODELS
from ppo_socket_server import response
from sm_reader import SMReader
from write_compute import write

def main():
 root=Path(__file__).resolve().parent;p=BasicPolicy(root);s=json.loads((root/'sample_input.json').read_text(encoding='utf-8'))
 now=time.time()*1000;s['timestamp_ms']=now;s['compute']['timestamp_ms']=now
 for model in MODELS:
  for sm in range(24,81,8):
   s['compute'].update(model_id=model,allocated_sm=sm);x=encode(s,now_ms=now);assert x.shape==(20,)
   assert abs(x[-2].item()-MODELS.index(model)/4)<1e-6 and abs(x[-1].item()-sm/80)<1e-6
   q=p.predict(s,now)['service_prbs'];assert sum(q.values())==273 and min(q.values())>=5
 mutations=[lambda x:x.pop('compute'),lambda x:x['compute'].update(model_id='bad'),lambda x:x['compute'].update(allocated_sm=81),
            lambda x:x['compute'].update(allocated_sm=True),lambda x:x['compute'].update(allocated_sm=72.5),
            lambda x:x['compute'].update(timestamp_ms=now-2001),lambda x:x['compute'].update(timestamp_ms=now+6),
            lambda x:x['compute'].update(timestamp_ms=float('nan')),lambda x:x.update(timestamp_ms=now-201),
            lambda x:x['service_prbs'].update(yolo=200),lambda x:x.update(window_ms=70)]
 for mutate in mutations:
  z=copy.deepcopy(s);mutate(z)
  try:p.predict(z,now)
  except (ValueError,KeyError,TypeError):pass
  else:raise AssertionError('Invalid telemetry accepted')
 fields=[str(now),'100']
 for u in ('yolo_1','yolo_2','live_1','live_2'):
  fields.extend(str(s['ues'][u][k]) for k in ('ul_buffer_bytes','ul_snr_db','ul_tp_mbps','ul_prb_avg'))
 msg=('PPO4R 7 '+' '.join(fields)+f" {s['service_prbs']['yolo']} {s['service_prbs']['live']}\n").encode('ascii')
 with tempfile.TemporaryDirectory() as d:
  path=Path(d)/'compute.json';write(path,'26x',80,now);reader=SMReader(path)
  reply=response(msg,p,5,now_ms=now,gpu_reader=reader).decode().split();expected=p.predict(s,now)['service_prbs']
  assert reply[:2]==['7','OK'] and list(map(int,reply[2:4]))==[expected['yolo'],expected['live']]
  write(path,'26x',80,now-3000)
  try:response(msg,p,5,now_ms=now,gpu_reader=reader)
  except ValueError:pass
  else:raise AssertionError('Stale bridge input accepted')
 durations=[]
 for _ in range(20):p.predict(s,now)
 for _ in range(300):
  t=time.perf_counter_ns();p.predict(s,now);durations.append((time.perf_counter_ns()-t)/1e6)
 durations.sort();r=dict(valid_model_sm_cases=40,invalid_cases=len(mutations),bridge_contract='PASS',cpu_predict_median_ms=durations[150],cpu_predict_p95_ms=durations[284],scope='local CPU only; not Linux socket or E2/RF integration')
 print(json.dumps(r,indent=2));return r

if __name__=='__main__':main()
