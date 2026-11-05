#!/bin/bash
# setup_venvs.sh - the two Python 3.10 environments (versions pinned from the lab host, 2026-11-06):
#   va_service/yolo_env                    VA (:8000) and non-VA (:8001) services: FastAPI/uvicorn, Pillow, ultralytics+torch (used only by
#                                          VA_BACKEND=local and bg_yolo.py; the default VA_BACKEND=dapp sends frames to the L1's dApp agent)
#   xapp_ppo/xapp_basic_no_lending/.venv   PPO actor runtime (torch CPU) shared by every PPO bundle (xapp.sh PPO_PYTHON)
# PYTHON=python3.10 by default (apt: python3.10 python3.10-venv).
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); PY=${PYTHON:-python3.10}
$PY -c 'import sys; assert sys.version_info[:2] == (3, 10), sys.version' 2>/dev/null || { echo "!! $PY is not Python 3.10 (PYTHON=... to override)"; exit 1; }
V=$D/va_service/yolo_env; [ -x $V/bin/python ] || $PY -m venv $V
$V/bin/pip install -q --upgrade pip && $V/bin/pip install -q -r $D/va_service/requirements.txt && echo "va_service/yolo_env: ok ($($V/bin/pip list 2>/dev/null | wc -l) packages)"
P=$D/xapp_ppo/xapp_basic_no_lending/.venv; [ -x $P/bin/python ] || $PY -m venv $P
$P/bin/pip install -q --upgrade pip && $P/bin/pip install -q -r $D/xapp_ppo/requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu && echo "xapp_ppo venv: ok ($($P/bin/pip list 2>/dev/null | wc -l) packages)"
