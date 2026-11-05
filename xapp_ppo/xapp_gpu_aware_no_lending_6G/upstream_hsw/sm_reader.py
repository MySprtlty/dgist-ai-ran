"""Transport adapter: producer atomically replaces a timestamped JSON file."""
import json,os
from pathlib import Path

class SMReader:
    def __init__(self,path=None):
        value=path or os.environ.get('PPO_SM_INPUT_FILE')
        if not value:raise ValueError('Set PPO_SM_INPUT_FILE to the external compute telemetry JSON')
        self.path=Path(value)
    def snapshot(self):
        try:
            value=json.loads(self.path.read_text(encoding='utf-8-sig'))
        except (OSError,json.JSONDecodeError) as exc:
            raise ValueError(f'Cannot read SM telemetry: {exc}') from exc
        if not isinstance(value,dict):raise ValueError('SM telemetry must be an object')
        return value
    def close(self):pass
