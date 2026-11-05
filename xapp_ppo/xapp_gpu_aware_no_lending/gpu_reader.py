"""Host physical GPU telemetry, read off the 100ms inference critical path.

NVML's utilization is its own hardware sampling window, NOT an instantaneous
SM allocation percentage. VRAM is recorded, never passed to the actor.
"""
import math
import os
import threading
import time


class GpuReader:
    def __init__(self, provider=None, clock=time.monotonic, wall=time.time):
        self.clock,self.wall=clock,wall
        self.lock=threading.Lock()
        self.stop_event=threading.Event()
        self.latest=None
        self.error=None
        self.nvml=None
        self.device=os.environ.get('PPO_GPU_DEVICE','0')
        if provider is None:
            try:
                import pynvml
                self.nvml=pynvml
                pynvml.nvmlInit()
                handle=(pynvml.nvmlDeviceGetHandleByIndex(int(self.device))
                        if self.device.isdecimal() else pynvml.nvmlDeviceGetHandleByUUID(self.device))
                def provider():
                    util=pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
                    mem=pynvml.nvmlDeviceGetMemoryInfo(handle)
                    return float(util),float(mem.used)/2**30
            except Exception as exc:
                if self.nvml:
                    try:self.nvml.nvmlShutdown()
                    except Exception:pass
                raise ValueError(f'GPU telemetry unavailable: {exc}; install nvidia-ml-py and check PPO_GPU_DEVICE') from exc
        self.provider=provider
        self.poll()
        self.snapshot()  # fail startup rather than fabricate a zero GPU sample
        self.thread=threading.Thread(target=self._run,name='ppo-gpu-telemetry',daemon=True)
        self.thread.start()

    def poll(self):
        try:
            util,mem=self.provider()
            if not math.isfinite(util) or not 0<=util<=100 or not math.isfinite(mem) or mem<0:
                raise ValueError('Invalid NVML measurement')
            with self.lock:
                self.latest=dict(util_percent=util,memory_used_gib=mem,
                                 polled_monotonic=self.clock(),polled_wall_ms=self.wall()*1000)
                self.error=None
        except Exception as exc:
            with self.lock:self.error=str(exc)

    def _run(self):
        while not self.stop_event.wait(.1):self.poll()

    def snapshot(self):
        with self.lock:
            row=dict(self.latest) if self.latest else None
            error=self.error
        if error or row is None:raise ValueError(f'GPU telemetry unavailable: {error}')
        age=(self.clock()-row.pop('polled_monotonic'))*1000
        if not 0<=age<=500:raise ValueError(f'GPU telemetry stale ({age:.0f}ms)')
        row.update(age_ms=age,device=self.device)
        return row

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1.)
        # Never shut down NVML underneath a stuck polling call.
        if self.nvml and not self.thread.is_alive():
            self.nvml.nvmlShutdown()

if __name__=='__main__':
    import json
    reader=GpuReader()
    try:print(json.dumps(reader.snapshot(),indent=2))
    finally:reader.close()

