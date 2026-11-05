"""One-shot adapter; a real producer must refresh timestamped telemetry regularly."""
import argparse,json,os,time
from pathlib import Path

def write(path,model,sm,timestamp_ms=None):
 if model not in ('26n','26s','26m','26l','26x') or type(sm)is not int or not 24<=sm<=80:raise ValueError('Invalid model/SM')
 dest=Path(path);dest.parent.mkdir(parents=True,exist_ok=True)
 temp=dest.with_name(dest.name+f'.{os.getpid()}.tmp')
 try:
  temp.write_text(json.dumps(dict(model_id=model,allocated_sm=sm,timestamp_ms=time.time()*1000 if timestamp_ms is None else timestamp_ms)),encoding='utf-8')
  os.replace(temp,dest)
 finally:temp.unlink(missing_ok=True)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--model',required=True);p.add_argument('--sm',type=int,required=True);a=p.parse_args();write(a.output,a.model,a.sm)
