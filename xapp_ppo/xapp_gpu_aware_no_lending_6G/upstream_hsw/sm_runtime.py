"""CPU-only 6G direct model/SM policy. No training or simulation dependency."""
import hashlib,json,math
from pathlib import Path
import torch
from radio_runtime import encode as radio_encode

MODELS=('26n','26s','26m','26l','26x')
SCHEMA='ppo20-radio18-modelscalar1-sm1-ensemble-v1'

class Actor(torch.nn.Module):
 def __init__(self,dim=20):
  super().__init__()
  self.net=torch.nn.Sequential(torch.nn.Linear(dim,128),torch.nn.Tanh(),torch.nn.Linear(128,128),torch.nn.Tanh())
  self.head=torch.nn.Linear(128,264)
  self.register_buffer('mean_obs',torch.zeros(dim));self.register_buffer('std_obs',torch.ones(dim))
 def logits(self,x):return self.head(self.net(((x-self.mean_obs)/self.std_obs).clamp(-10,10)))

class Ensemble(torch.nn.Module):
 def __init__(self,count=3):super().__init__();self.actors=torch.nn.ModuleList([Actor() for _ in range(count)])
 def forward(self,x):
  probs=torch.stack([a.logits(x).softmax(-1) for a in self.actors]).mean(0)
  return probs.argmax(-1)+5

def encode(sample,total_prbs=273,min_prbs=5,now_ms=None,window_tolerance_ms=0.):
 if not isinstance(sample,dict):raise ValueError('sample must be an object')
 radio=dict(sample);compute=radio.pop('compute',None)
 if not isinstance(compute,dict) or set(compute)!={'model_id','allocated_sm','timestamp_ms'}:
  raise ValueError('compute requires one atomic model_id,allocated_sm,timestamp_ms tuple')
 model=compute['model_id'];sm=compute['allocated_sm'];stamp=compute['timestamp_ms']
 if model not in MODELS:raise ValueError('model_id must be26n/26s/26m/26l/26x')
 if type(sm) is not int or not 24<=sm<=80:raise ValueError('allocated_sm must be integer24..80')
 if isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp) or stamp<0:raise ValueError('Invalid compute timestamp')
 ref=sample.get('timestamp_ms') if now_ms is None else now_ms
 if isinstance(ref,bool) or not isinstance(ref,(int,float)) or not math.isfinite(ref):raise ValueError('Invalid reference timestamp')
 if not -5<=ref-stamp<=2000:raise ValueError('Stale/future compute input; retain confirmed budget')
 radio_x=radio_encode(radio,total_prbs,min_prbs,now_ms,window_tolerance_ms=window_tolerance_ms)
 return torch.cat([radio_x,torch.tensor([MODELS.index(model)/4.,sm/80.],dtype=torch.float32)])

class BasicPolicy:
 def __init__(self,bundle=None,num_threads=1):
  self.bundle=Path(bundle) if bundle else Path(__file__).resolve().parent
  m=json.loads((self.bundle/'manifest.json').read_text(encoding='utf-8'))
  if (m.get('schema')!=SCHEMA or m.get('input_dim')!=20 or m.get('model_ids')!=list(MODELS)
      or m.get('total_ul_prbs')!=273 or m.get('min_slice_prbs')!=5
      or m.get('borrow_unused_prbs') is not False or m.get('ensemble_size')!=3
      or m.get('model_code_scale')!=4 or m.get('sm_scale')!=80):raise ValueError('Incompatible model contract')
  path=self.bundle/'actor.pt'
  if hashlib.sha256(path.read_bytes()).hexdigest()!=m['actor_sha256']:raise ValueError('Actor checksum mismatch')
  torch.set_num_threads(num_threads);self.actor=Ensemble(3)
  self.actor.load_state_dict(torch.load(path,map_location='cpu',weights_only=True),strict=True);self.actor.eval()
  for a in self.actor.actors:
   if not all(torch.isfinite(x).all() for x in a.state_dict().values()) or not (a.std_obs>0).all():raise ValueError('Invalid model tensors')
 def predict(self,sample,now_ms,*,window_tolerance_ms=0.):
  with torch.inference_mode():q=int(self.actor(encode(sample,now_ms=now_ms,window_tolerance_ms=window_tolerance_ms)).item())
  if not 5<=q<=268:raise ValueError('Invalid policy budget')
  budgets={'yolo':q,'live':273-q}
  return dict(schema=SCHEMA,telemetry_timestamp_ms=sample['timestamp_ms'],control_interval_ms=100,total_ul_prbs=273,
              borrow_unused_prbs=False,service_prbs=budgets,service_ratios={s:v/273 for s,v in budgets.items()},
              compute=dict(sample['compute']))

if __name__=='__main__':
 import argparse,time
 p=argparse.ArgumentParser();p.add_argument('--input',type=Path,default=Path(__file__).with_name('sample_input.json'));p.add_argument('--offline',action='store_true');a=p.parse_args()
 sample=json.loads(a.input.read_text(encoding='utf-8'))
 print(json.dumps(BasicPolicy().predict(sample,sample['timestamp_ms'] if a.offline else time.time()*1000),indent=2))
