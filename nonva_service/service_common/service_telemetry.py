"""Same-host, per-service/UE telemetry; single Uvicorn worker per service.

Atomic snapshots carry source freshness, session identity and measurement age.
No radio control and no GPU sampling. Browser E2E is reported AFTER the response.
"""
from collections import deque
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid


def telemetry_dir():
    return Path(os.environ.get('SERVICE_TELEMETRY_DIR',
                              str(Path(tempfile.gettempdir())/'oai-service-telemetry')))


class TelemetryStore:
    def __init__(self, service, directory=None, clock=None):
        if service not in ('va', 'nonva'):
            raise ValueError('Unknown service')
        self.service = service
        self.directory = Path(directory) if directory else telemetry_dir()
        self.clock = clock or (lambda: time.time_ns()/1e6)
        self.lock = threading.RLock()
        self.rows = {u: self._idle(u) for u in ('ue1','ue2')}
        self.stop_event = threading.Event()
        self.thread = None

    def _idle(self, ue):
        return dict(schema='service-latency-v1',service=self.service,ue_id=ue,
                    session_token=None,active=False,heartbeat_ms=None,
                    latency_ms=None,sample_ms=None,frame_id=-1,failures=0,
                    status='idle',events=deque(),seen=deque(maxlen=256))

    def _ue(self, ue):
        if ue not in self.rows: raise ValueError('UE must be ue1 or ue2')
        return self.rows[ue]

    def start(self, ue):
        with self.lock:
            row = self._ue(ue)
            # Explicit Stop or server restart is required to release a label.
            # Never silently reassign an active phone whose radio is delayed.
            if row['active']: raise ValueError(f'{ue} is already active; stop that session first')
            row = self._idle(ue)
            row.update(active=True,session_token=uuid.uuid4().hex,
                       heartbeat_ms=self.clock(),status='pending')
            self.rows[ue] = row
            self.flush()
            return row['session_token']

    def check(self, ue, token):
        row = self._ue(ue)
        if not row['active'] or not token or row['session_token'] != token:
            raise ValueError('Inactive or obsolete UE session')
        return row

    def heartbeat(self, ue, token):
        with self.lock:
            self.check(ue,token)['heartbeat_ms'] = self.clock()

    def result(self, ue, token, frame_id, latency_ms=None, payload_bytes=0,
               sample_ms=None, success=True):
        with self.lock:
            row = self.check(ue,token)
            now = self.clock()
            if type(frame_id) is not int or frame_id < 0:
                raise ValueError('Invalid frame ID')
            if type(payload_bytes) is not int or payload_bytes < 0:
                raise ValueError('Invalid byte count')
            if type(success) is not bool: raise ValueError('Invalid success flag')
            if success:
                if (isinstance(latency_ms,bool) or not isinstance(latency_ms,(int,float))
                        or not math.isfinite(latency_ms) or not 0 <= latency_ms <= 60000):
                    raise ValueError('Invalid latency')
                stamp = now if sample_ms is None else sample_ms
                if (not isinstance(stamp,(int,float)) or not math.isfinite(stamp)
                        or stamp > now+50 or stamp < now-10000):
                    raise ValueError('Invalid completion timestamp / clock offset')
                stamp = min(now,stamp)
            # Concurrent requests can complete out of capture order. Use the
            # completion timestamp, not increasing capture ID, for latest latency.
            if frame_id in row['seen']: return False
            row['seen'].append(frame_id)
            if not success: stamp=now
            if success: row['events'].append((now,payload_bytes))
            if row['sample_ms'] is not None and (stamp < row['sample_ms'] or
                    (stamp == row['sample_ms'] and frame_id < row['frame_id'])):
                return False
            row['frame_id'] = frame_id
            if success:
                row.update(latency_ms=float(latency_ms),sample_ms=float(stamp),status='valid')
            else:
                row.update(latency_ms=None,sample_ms=now,status='failed')
                row['failures'] += 1
            return True

    def stop(self, ue, token):
        with self.lock:
            row = self._ue(ue)
            if not token or row['session_token'] != token:
                raise ValueError('Obsolete UE session')
            row.update(active=False,status='idle',latency_ms=None,sample_ms=None)
            self.flush()

    def flush(self):
        with self.lock:
            self.directory.mkdir(parents=True,exist_ok=True)
            now = self.clock()
            for ue,row in self.rows.items():
                while row['events'] and row['events'][0][0] < now-1000:
                    row['events'].popleft()
                snap = {k:v for k,v in row.items() if k not in ('events','seen')}
                snap['written_ms'] = now
                snap['app_tp_1s_mbps'] = sum(b for _,b in row['events'])*8/1e6
                if row['active'] and now-row['heartbeat_ms'] > 3000:
                    snap['status'] = 'stale'
                path = self.directory/f'{self.service}_{ue}.json'
                tmp = path.with_suffix(f'.{os.getpid()}.tmp')
                tmp.write_text(json.dumps(snap,allow_nan=False),encoding='utf-8')
                os.replace(tmp,path)

    def start_writer(self):
        self.flush()
        def work():
            while not self.stop_event.wait(.1):
                try: self.flush()
                except OSError as exc: print(f'[TELEMETRY] snapshot write failed: {exc}',flush=True)
        self.thread = threading.Thread(target=work,daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=1)
        # Do not write idle on process exit: loss of server is not a stopped UE.


def attach_telemetry(app, service):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    store = TelemetryStore(service)
    app.router.add_event_handler('startup',store.start_writer)
    app.router.add_event_handler('shutdown',store.close)

    @app.get('/telemetry_client.js')
    async def client_script():
        return FileResponse(Path(__file__).with_name('service_telemetry_client.js'),
                            media_type='application/javascript')

    @app.post('/telemetry/{action}')
    async def telemetry(action: str, body: dict):
        try:
            ue,token = body.get('ue_id'),body.get('session_token')
            if action == 'start':
                return dict(session_token=store.start(ue),ue_id=ue)
            if action == 'heartbeat': store.heartbeat(ue,token)
            elif action == 'stop': store.stop(ue,token)
            elif action == 'result':
                store.result(ue,token,body['frame_id'],body.get('latency_ms'),
                             body.get('payload_bytes',0),body.get('sample_ms'),body.get('success',True))
            else: raise ValueError('Unknown action')
            return {'ok':True}
        except (ValueError,KeyError,TypeError) as exc:
            raise HTTPException(status_code=409,detail=str(exc)) from exc
    return store

