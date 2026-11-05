"""Join application latency to MAC telemetry using the configured UE order.

VA UE1/2 = first/second VA_RNTIS; NonVA UE1/2 = first/second NONVA_RNTIS.
This is an experiment mapping, NOT automatic SIM/RNTI discovery.
"""
import json
import math
import os
from pathlib import Path
import tempfile

MAPPING = {'yolo_1':('va','ue1'), 'yolo_2':('va','ue2'),
           'live_1':('nonva','ue1'), 'live_2':('nonva','ue2')}


class LatencyReader:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('SERVICE_TELEMETRY_DIR',
                              str(Path(tempfile.gettempdir())/'oai-service-telemetry')))

    def augment(self, sample, now_ms):
        details = {}
        for name,(service,ue) in MAPPING.items():
            try:
                row=json.loads((self.directory/f'{service}_{ue}.json').read_text(encoding='utf-8'))
            except (OSError,ValueError) as exc:
                raise ValueError(f'{service}/{ue}: no readable latency snapshot; start both service servers') from exc
            if (row.get('schema')!='service-latency-v1' or row.get('service')!=service
                    or row.get('ue_id')!=ue or type(row.get('active')) is not bool):
                raise ValueError(f'{name}: mismatched service telemetry')
            def finite(key):
                v=row.get(key)
                if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v):
                    raise ValueError(f'{name}: invalid {key}')
                return v
            age=now_ms-finite('written_ms')
            if not -50<=age<=500:
                raise ValueError(f'{name}: source stale ({age:.0f}ms); retain current quotas')
            state=row.get('status'); sample_age=None
            if not row['active']:
                if state!='idle': raise ValueError(f'{name}: invalid idle state')
                signal=-1.
            else:
                if not row.get('session_token'): raise ValueError(f'{name}: missing session token')
                heartbeat_age=now_ms-finite('heartbeat_ms')
                if state=='stale' or not -50<=heartbeat_age<=3000:
                    raise ValueError(f'{name}: client heartbeat stale; not considered idle')
                signal=-2.
                if state=='valid':
                    sample_age=now_ms-finite('sample_ms')
                    latency=finite('latency_ms')
                    if not 0<=latency<=60000 or sample_age < -50:
                        raise ValueError(f'{name}: invalid measured latency')
                    if sample_age<=1000: signal=latency
                    else: state='pending'
                elif state not in ('pending','failed'):
                    raise ValueError(f'{name}: unknown latency state')
            sample['ues'][name]['service_latency_ms']=signal
            details[name]=dict(state=state,latency_signal_ms=signal,sample_age_ms=sample_age,
                               source_age_ms=age,app_tp_1s_mbps=row.get('app_tp_1s_mbps'),
                               session_token=row.get('session_token'),frame_id=row.get('frame_id'))
        return details

