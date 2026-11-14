#!/usr/bin/env python3
"""Component control of the dashboard (2026-11-14): On / Off / restart of L1 + gNB, 5G core, VA, non-VA, xApp and the traffic generator.

The dashboard does not reimplement anything: every button runs one of the operator scripts of this package (dgist.sh, core.sh, va.sh,
nonva.sh, xapp.sh, tgen.sh) in the package directory as the account the dashboard runs under (nvidia), and shows the script's own output and
exit code. What it adds is the safety around them:

  - two steps: POST /api/ctl/plan answers with the exact command lines, the warnings and the reasons an action is refused, plus a token;
    POST /api/ctl/run with that token (valid 60 s, checked again against the state of that moment) starts it. The page shows the plan in a
    confirmation dialog in between.
  - one action at a time (409 while one runs), POST + JSON only, same-origin only, never from the UE network (dash_server's 403 / TLS deny).
  - a run name for every start of L1 + gNB, refused when runs/<name> exists (a run name is never used twice).
  - dependencies are refused with the reason instead of tried: L1 and gNB are ONE switch (docker compose: L1 -> healthy -> gNB), Off / restart
    of it needs the xApp stopped first, the core cannot go down under a running gNB, the traffic generator switch only writes .env and takes
    effect at the next L1 start, core.sh needs sudo (without password-less sudo the buttons stay off and the terminal command is shown).
  - the GPU mode (MIG) is shown read-only: switching it needs root and is not offered here.
  - every command is logged: runs/dash/control.jsonl (time, component, action, command, exit code, duration, client, dry-run) and its output in
    runs/dash/control/<time>_<component>_<action>.log.

DASH_CTL=0 switches the feature off. DASH_CTL_DRYRUN=1 (always in the simulation) writes the command line to the log instead of running it.
"""
import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
LOADS = ("low", "mid", "high", "auto")
VERB = {"on": "start", "off": "stop", "restart": "restart"}
TOKEN_S = 60.0
J = dict(ensure_ascii=False, separators=(",", ":"))

# what the page lists: component, the status lamps it shows, its actions. The command text of each action comes from _plan().
COMPS = (
    {"id": "ran", "label": "L1 + gNB", "lamps": [["l1", "L1 (cuphycontroller)"], ["gnb", "gNB (OAI)"]], "actions": ["on", "off", "restart"],
     "dep": "L1과 gNB는 한 스위치입니다 (docker compose: L1이 healthy가 된 뒤 gNB가 뜸). 켤 때마다 새 실험 이름이 필요합니다."},
    {"id": "core", "label": "5G 코어 (Open5GS)", "lamps": [["core", "Open5GS"]], "actions": ["on", "off", "restart"],
     "dep": "core.sh는 sudo가 필요합니다. gNB가 떠 있는 동안에는 끌 수 없습니다. 연구실과 함께 쓰는 코어입니다."},
    {"id": "va", "label": "VA 서비스", "lamps": [["va", "VA :8000"]], "actions": ["on", "off", "restart"],
     "dep": "끄거나 재시작하면 폰 페이지를 새로 고쳐야 합니다. 추론은 L1 안의 dApp 에이전트가 떠 있어야 됩니다."},
    {"id": "nonva", "label": "Non-VA 서비스", "lamps": [["nonva", "Non-VA :8001"]], "actions": ["on", "off", "restart"],
     "dep": "끄거나 재시작하면 폰 페이지를 새로 고쳐야 합니다."},
    {"id": "xapp", "label": "xApp (PPO 슬라이싱)", "lamps": [["xapp", "PPO xApp"], ["ric", "nearRT-RIC (읽기 전용)"]], "actions": ["on", "off", "restart"],
     "dep": "gNB와 nearRT-RIC가 떠 있고 폰 4대가 모두 붙어 있어야 시작됩니다. Ours 모드 전용."},
    {"id": "tgen", "label": "트래픽 생성기 (5셀)", "lamps": [["tgen", "RU 에뮬레이터 + testMAC"]], "actions": ["on", "off", "restart", "load"],
     "dep": "On / Off는 .env TRAFFIC_GEN만 바꿉니다: 실제 적용은 다음 L1 시작 때 (L1 + gNB 재시작 = 폰 재접속). 부하 단계는 도는 중에 바로 바뀝니다."},
)


class Refused(Exception):
    def __init__(self, code, msg):
        super().__init__(msg); self.code = code


class Control:
    def __init__(self, root, hub, cfg, base, log, dry=False):
        self.root = Path(root); self.hub = hub; self.cfg = cfg; self.log = log; self.dry = bool(dry)
        self.dir = Path(base) / "control"; self.jsonl = Path(base) / "control.jsonl"
        self.lock = threading.Lock(); self.busy = None; self.last = None; self.plans = {}; self.hist = deque(maxlen=40); self._sudo = (0.0, False)

    # ------------------------------------------------------------------ what is up now
    def _can_sudo(self):
        v = os.environ.get("DASH_CTL_SUDO", "auto")
        if v in ("0", "1"): return v == "1"
        now = time.time()
        if now - self._sudo[0] > 600.0:                            # asked rarely: every try is a line in the auth log
            try: ok = subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
            except Exception: ok = False
            self._sudo = (now, ok)
        return self._sudo[1]

    def _now(self):
        with self.hub.lock: s = dict(self.hub.sysd)
        try: run = (self.root / "runs" / "current_run").read_text().strip() or None
        except OSError: run = None
        if self.hub.sim: run = "sim_run" if (s.get("up") or {}).get("l1") else None
        return {"mode": s.get("mode") or self.cfg.get("mode"), "up": dict(s.get("up") or {}), "tgen": dict(s.get("tgen") or {}), "run": run,
                "can_sudo": self._can_sudo(), "rec": (self.hub.rec_info() or {}).get("name")}

    # ------------------------------------------------------------------ one action -> command lines, refusals, warnings
    def _plan(self, comp, action, arg, st):
        up = st["up"]; on = lambda k: bool(up.get(k)); off = lambda k: up.get(k) is False
        cmds = []; no = []; warn = []; need = None
        if st["rec"]: warn.append("측정 중입니다 (%s): 이 동작의 영향이 기록에 그대로 남습니다." % st["rec"])
        if comp == "ran":
            if action in ("on", "restart"):
                need = "name"
                if arg is not None:
                    if not NAME_RE.match(arg): no.append("실험 이름: 영문·숫자와 _ - . 로 3~64자, 첫 글자는 영문 또는 숫자.")
                    elif (self.root / "runs" / arg).exists(): no.append("runs/%s 가 이미 있습니다. 실험 이름은 다시 쓰지 않습니다: 새 이름을 주세요." % arg)
                if off("core"): no.append("5G 코어가 꺼져 있습니다: 먼저 코어를 켜세요.")
                if off("ric") and st["mode"] != "baseline": warn.append("nearRT-RIC가 꺼져 있습니다: gNB가 E2 연결 없이 떠서 xApp을 쓸 수 없습니다 (터미널: ./ric.sh start 를 먼저).")
                warn.append("시작에 몇 분 걸립니다 (L1 healthy 대기 -> gNB). 폰 4대는 그 뒤에 다시 붙습니다.")
                if (st["tgen"] or {}).get("on"): warn.append("트래픽 생성기 스위치가 켜져 있습니다: L1이 6셀(폰 셀 + 에뮬레이터 5셀)로 뜹니다.")
            if action == "on":
                if on("l1") or on("gnb"): no.append("L1 / gNB가 이미 떠 있습니다 (run %s). 다시 띄우려면 '재시작'." % (st["run"] or "?"))
                elif st["run"]: no.append("runs/current_run 이 남아 있습니다 (%s): 먼저 Off (./dgist.sh down)로 정리하세요." % st["run"])
                cmds = [["./dgist.sh", "up", arg or "<실험 이름>"]]
            elif action in ("off", "restart"):
                if action == "off" and off("l1") and off("gnb") and not st["run"]: no.append("L1 / gNB가 이미 꺼져 있습니다.")
                if on("xapp"): no.append("xApp이 실행 중입니다: 먼저 xApp을 끄세요 (quota 제거 후 gNB를 내려야 함).")
                warn.append("폰 4대의 접속이 끊깁니다. VA 서비스의 dApp 연결(L1 안의 에이전트)도 끊깁니다.")
                cmds = [["./dgist.sh", "down"]] + ([["./dgist.sh", "up", arg or "<실험 이름>"]] if action == "restart" else [])
        elif comp == "core":
            if action not in VERB: raise Refused(400, "unknown action")
            cmds = {"on": [["./core.sh", "up"]], "off": [["./core.sh", "down"]], "restart": [["./core.sh", "down"], ["./core.sh", "up"]]}[action]
            if not st["can_sudo"]:
                no.append("core.sh는 sudo(systemctl, iptables)가 필요한데, 대시보드를 실행하는 계정에는 비밀번호 없는 sudo가 없습니다. 터미널에서 직접 실행하세요:  cd %s && %s"
                          % (self.root, " && ".join(" ".join(c) for c in cmds)))
            if action == "on" and on("core"): no.append("코어가 이미 켜져 있습니다.")
            if action == "off" and off("core"): no.append("코어가 이미 꺼져 있습니다.")
            if action in ("off", "restart"):
                if on("gnb") or on("l1"): no.append("L1 / gNB가 떠 있습니다: 먼저 L1 + gNB를 끄세요.")
                warn.append("연구실과 함께 쓰는 코어입니다. 내리면 모든 단말의 등록이 사라지고, 다시 올리면 폰이 새로 등록해야 합니다.")
        elif comp in ("va", "nonva"):
            if action not in VERB: raise Refused(400, "unknown action")
            sh = "./va.sh" if comp == "va" else "./nonva.sh"; nm = "VA" if comp == "va" else "Non-VA"
            cmds = [[sh, VERB[action]]]
            if action == "on" and on(comp): no.append("%s 서비스가 이미 떠 있습니다. 다시 띄우려면 '재시작'." % nm)
            if action == "off" and off(comp): no.append("%s 서비스가 이미 꺼져 있습니다." % nm)
            if action in ("off", "restart"):
                warn.append("폰에서 Stop을 누르기 전에 서비스가 내려가면 그 세션의 프레임 로그(CSV)는 저장되지 않습니다.")
                warn.append("다시 뜬 뒤에는 폰 페이지를 새로 고침하고 시작해야 합니다. 이 대시보드의 뷰어 화면도 그동안 끊깁니다.")
            if comp == "va" and action in ("on", "restart") and off("l1") and off("yolo"): warn.append("L1 (dApp 에이전트)이 꺼져 있습니다: 서비스는 뜨지만 추론 요청은 실패합니다.")
        elif comp == "xapp":
            if action not in VERB: raise Refused(400, "unknown action")
            cmds = [["./xapp.sh", VERB[action]]]
            if st["mode"] == "baseline": no.append("Baseline 모드에는 xApp이 없습니다.")
            if action == "on" and on("xapp"): no.append("xApp이 이미 실행 중입니다. 다시 띄우려면 '재시작'.")
            if action == "off" and off("xapp"): no.append("xApp이 이미 꺼져 있습니다.")
            if action in ("on", "restart"):
                if off("gnb"): no.append("gNB가 꺼져 있습니다: 먼저 L1 + gNB를 켜세요.")
                if off("ric"): no.append("nearRT-RIC가 꺼져 있습니다 (터미널: ./ric.sh start). RIC는 gNB보다 먼저 떠 있어야 E2가 연결됩니다.")
                warn.append("폰 4대가 모두 붙어 있어야 합니다. 아니면 스크립트가 'RNTI mapping not possible'로 끝납니다 (출력에 그대로 보임).")
            if action in ("off", "restart"): warn.append("xApp이 멈추면 UL PRB quota가 제거됩니다 (그동안 슬라이싱 없음).")
        elif comp == "tgen":
            tg = st["tgen"] or {}
            if action in ("on", "off"):
                cmds = [["./tgen.sh", action]]
                if tg.get("on") is (action == "on"): no.append("스위치가 이미 %s 입니다 (.env TRAFFIC_GEN)." % ("On" if action == "on" else "Off"))
                warn.append("이 버튼은 .env TRAFFIC_GEN만 바꿉니다. 지금 떠 있는 L1은 그대로이고, 다음 L1 시작 때 적용됩니다 (L1 + gNB 재시작 = 폰 4대 재접속).")
            elif action == "load":
                need = "load"; cmds = [["./tgen.sh", "load", arg if arg in LOADS else "<low|mid|high|auto>"]]
                if arg is not None and arg not in LOADS: no.append("부하 단계는 low, mid, high, auto 중 하나.")
                if not tg.get("run"): no.append("트래픽 생성기가 돌고 있지 않습니다 (L1이 6셀로 떠 있어야 부하 단계를 바꿀 수 있음).")
                warn.append("에뮬레이터 5셀의 상향(PUSCH) 부하가 약 0.3초 뒤 바뀝니다. L1 재시작은 없습니다.")
            elif action == "restart":
                cmds = []; no.append("트래픽 생성기는 L1 컨테이너 안에서 L1과 함께 뜹니다: 단독 재시작이 없습니다. 'L1 + gNB 재시작'을 쓰세요.")
            else: raise Refused(400, "unknown action")
        else: raise Refused(400, "unknown component")
        if not cmds and not no: raise Refused(400, "unknown action")
        return {"comp": comp, "action": action, "arg": arg, "need": need, "commands": [" ".join(c) for c in cmds], "argv": cmds, "blocked": no, "warnings": warn}

    def state(self):
        st = self._now(); comps = []
        for c in COMPS:
            acts = []
            for a in c["actions"]:
                p = self._plan(c["id"], a, None, st)
                acts.append({"id": a, "need": p["need"], "commands": p["commands"], "blocked": p["blocked"]})
            comps.append({"id": c["id"], "label": c["label"], "dep": c["dep"], "lamps": [{"key": k, "label": lb, "up": st["up"].get(k)} for k, lb in c["lamps"]], "actions": acts})
        with self.lock:
            busy = self._view(self.busy); last = self._view(self.last); hist = list(self.hist)[-15:]
        return {"on": True, "dry": self.dry, "mode": st["mode"], "run": st["run"], "tgen": st["tgen"], "can_sudo": st["can_sudo"], "rec": st["rec"], "root": str(self.root),
                "comps": comps, "busy": busy, "last": last, "hist": hist, "log": str(self.jsonl), "loads": list(LOADS), "t": time.time()}

    def _view(self, job):
        if job is None: return None
        out = {k: job.get(k) for k in ("id", "comp", "action", "arg", "commands", "step", "t0", "t1", "rc", "pid", "note", "dry", "client", "log")}
        try:
            with open(job["path"], "rb") as f:
                f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 6000)); out["tail"] = f.read().decode("utf-8", "replace")
        except OSError: out["tail"] = ""
        return out

    # ------------------------------------------------------------------ plan -> run
    def plan(self, comp, action, arg=None):
        arg = None if arg in (None, "") else str(arg).strip()
        p = self._plan(str(comp), str(action), arg, self._now())
        if p["need"] and arg is None: p["blocked"] = p["blocked"] + (["실험 이름이 필요합니다."] if p["need"] == "name" else ["부하 단계를 고르세요."])
        p["dry"] = self.dry; p["busy"] = self.busy is not None; p["token"] = None
        if not p["blocked"]:
            tok = secrets.token_urlsafe(18); now = time.time()
            with self.lock:
                for k in [k for k, v in self.plans.items() if v[0] < now]: del self.plans[k]
                self.plans[tok] = (now + TOKEN_S, p["comp"], p["action"], p["arg"])
            p["token"] = tok; p["valid_s"] = TOKEN_S
        del p["argv"]
        return p

    def run(self, token, client="?"):
        with self.lock:
            ent = self.plans.pop(str(token), None)
            if ent is None or ent[0] < time.time(): raise Refused(410, "확인 시간이 지났습니다. 다시 눌러 주세요.")
            if self.busy is not None: raise Refused(409, "다른 동작이 실행 중입니다 (%s %s)." % (self.busy["comp"], self.busy["action"]))
            p = self._plan(ent[1], ent[2], ent[3], self._now())                                   # the state may have changed since the dialog opened
            if p["blocked"]: raise Refused(409, " / ".join(p["blocked"]))
            stamp = time.strftime("%Y%m%d_%H%M%S"); self.dir.mkdir(parents=True, exist_ok=True)
            job = {"id": "%s_%s_%s" % (stamp, p["comp"], p["action"]), "comp": p["comp"], "action": p["action"], "arg": p["arg"], "commands": p["commands"], "argv": p["argv"],
                   "step": 0, "t0": time.time(), "t1": None, "rc": None, "pid": None, "note": None, "dry": self.dry, "client": client}
            job["path"] = str(self.dir / (job["id"] + ".log")); job["log"] = "control/" + job["id"] + ".log"
            self.busy = job
        self.log("control: %s %s %s by %s%s" % (job["comp"], job["action"], job["arg"] or "", client, " (dry-run)" if self.dry else ""))
        threading.Thread(target=self._exec, args=(job,), name="dash-control", daemon=True).start()
        return self._view(job)

    def _exec(self, job):
        env = {"HOME": os.environ.get("HOME", "/"), "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
               "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin", "LANG": os.environ.get("LANG", "C.UTF-8"), "TERM": "dumb"}
        limit = 1500.0 if job["comp"] == "ran" else 300.0; rc = None; note = None
        try:
            for i, argv in enumerate(job["argv"]):
                job["step"] = i + 1; t0 = time.time(); line = " ".join(argv)
                with open(job["path"], "ab") as f:
                    f.write(("$ cd %s && %s\n" % (self.root, line)).encode()); f.flush()
                    if self.dry:
                        f.write(("[dry-run] 실행하지 않았습니다. 실제로는 이 명령이 실행됩니다: %s\n" % line).encode()); rc = 0
                    else:
                        p = subprocess.Popen(["bash"] + argv, cwd=str(self.root), stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, start_new_session=True, env=env)
                        job["pid"] = p.pid
                        try: rc = p.wait(timeout=limit)
                        except subprocess.TimeoutExpired:
                            rc = None; note = "%.0f초가 지나도 끝나지 않았습니다 (pid %d, 계속 실행 중). 터미널에서 확인하세요." % (limit, p.pid)
                    f.write(("[exit %s, %.1f s]\n" % ("?" if rc is None else rc, time.time() - t0)).encode())
                self._journal(job, line, rc, time.time() - t0)
                if rc != 0:
                    if i + 1 < len(job["argv"]) and note is None: note = "앞 단계가 실패해 다음 명령(%s)은 실행하지 않았습니다." % " ".join(job["argv"][i + 1])
                    break
        except Exception as e:
            note = "실행 실패: %r" % (e,); rc = -1
            self._journal(job, " ".join(job["argv"][max(0, job["step"] - 1)]), rc, 0.0, note)
        with self.lock:
            job["rc"] = rc; job["t1"] = time.time(); job["note"] = note; job["pid"] = None
            self.hist.append({k: job.get(k) for k in ("id", "comp", "action", "arg", "commands", "t0", "t1", "rc", "note", "dry", "client", "log")})
            self.last = job; self.busy = None
        self.log("control: %s %s -> exit %s" % (job["comp"], job["action"], rc))

    def _journal(self, job, line, rc, dur, note=None):
        rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "t": round(time.time(), 3), "component": job["comp"], "action": job["action"], "arg": job["arg"], "command": line,
               "exit": rc, "dur_s": round(dur, 1), "client": job["client"], "dry_run": self.dry, "output": job["log"]}
        if note: rec["note"] = note
        try:
            with open(self.jsonl, "a") as f: f.write(json.dumps(rec, **J) + "\n")
        except OSError as e: self.log("control journal: %r" % (e,))
