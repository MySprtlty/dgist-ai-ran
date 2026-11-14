/* _dgist experiment dashboard - "제어" tab: On / Off / restart of the components (2026-11-14).
   The page never builds a command itself. A button asks the server for a plan (the script command lines, the warnings, the reasons an action
   is refused), shows it in a confirmation dialog, and only "실행" starts it. One action at a time; the script's own output and exit code are
   shown as they are. The GPU mode is shown read-only. */
'use strict';
(function () {
  const D = window.D, el = D.el, S = D.S;
  const ACT = { on: 'On', off: 'Off', restart: '재시작', load: '부하 단계' };
  const C = { root: null, body: null, st: null, on: false, timer: null, sig: '', err: '', jobEl: null, jobSig: '', tail: null };

  const nameOf = id => { const c = C.st && C.st.comps.find(x => x.id === id); return c ? c.label : id; };
  const hms = t => t ? D.clock(t) : '–';
  function rcTag(j) {
    if (j.rc === 0) return D.tag(j.dry ? 'dry-run · exit 0' : 'exit 0', 'good');
    return D.tag(j.rc == null ? '끝나지 않음' : 'exit ' + j.rc, 'bad');
  }

  async function poll() {
    try { C.st = await D.api('/api/ctl/state'); C.err = ''; } catch (e) { C.err = String(e.message || e); }
    draw(); arm();
  }
  function arm() {
    clearTimeout(C.timer); C.timer = null;
    if (C.on) C.timer = setTimeout(poll, C.st && C.st.busy ? 700 : 2000);
  }
  function shown(on) {
    on = !!on && !!C.root; if (on === C.on) return; C.on = on;
    if (on) poll(); else { clearTimeout(C.timer); C.timer = null; }
  }

  // ---------------------------------------------------------------- the confirmation dialog
  function ask(comp, action) {
    let plan = null, arg = null, seq = 0, deb = null, running = false;
    D.app.modal(false, (m, close) => {
      const c = C.st.comps.find(x => x.id === comp), danger = action !== 'on' && action !== 'load';
      const inp = el('input.field', { placeholder: '실험 이름 (예: ours_tg_auto_1114_1500)', maxlength: 64, spellcheck: 'false', autocomplete: 'off' });
      const loads = el('div.ctlloads'), cmd = el('pre.ctlcmd'), warn = el('div.ctlwarn'), no = el('div.err'), ban = el('div.ctlban'), extra = el('div');
      const go = el('button.btn' + (danger ? '.danger' : '.primary'), { disabled: true }, '실행');
      m.appendChild(el('h2', c.label + ' · ' + ACT[action]));
      m.appendChild(el('div.desc', c.dep));
      m.appendChild(ban); m.appendChild(extra);
      m.appendChild(el('div.ctllb', '실행할 명령 (' + C.st.root + ' 에서)')); m.appendChild(cmd); m.appendChild(warn); m.appendChild(no);
      m.appendChild(el('div.acts', el('button.btn.ghost', { onclick: close }, '취소'), go));
      function show(p) {
        plan = p; D.txt(cmd, p.commands.length ? p.commands.join('\n') : '(실행할 명령 없음)');
        warn.textContent = ''; for (const w of p.warnings) warn.appendChild(el('div', w));
        no.textContent = ''; for (const b of p.blocked) no.appendChild(el('div', b));
        if (p.busy) no.appendChild(el('div', '다른 동작이 실행 중입니다. 끝난 뒤에 실행할 수 있습니다.'));
        ban.hidden = !p.dry; D.txt(ban, p.dry ? 'DRY-RUN: 명령을 실행하지 않고, 명령줄만 기록에 남깁니다.' : '');
        go.disabled = running || !p.token || p.busy;
        if (p.need === 'name' && !inp.parentNode) { extra.appendChild(el('div.ctllb', '실험 이름 (runs/<이름> 으로 저장, 한 번 쓴 이름은 다시 못 씀)')); extra.appendChild(inp); setTimeout(() => inp.focus(), 30); }
        if (p.need === 'load' && !loads.firstChild) {
          extra.appendChild(el('div.ctllb', '부하 단계')); extra.appendChild(loads);
          for (const l of C.st.loads) loads.appendChild(el('button.btn.sm.ghost', { 'data-l': l, onclick: () => { arg = l; replan(); } }, l));
        }
        for (const b of D.$$('button', loads)) b.classList.toggle('sel', b.dataset.l === arg);
      }
      async function replan() {
        const n = ++seq; go.disabled = true;
        try { const p = await D.api('/api/ctl/plan', { comp, action, arg }); if (n === seq) show(p); }
        catch (e) { if (n === seq) { no.textContent = ''; no.appendChild(el('div', String(e.message || e))); } }
      }
      inp.addEventListener('input', () => { arg = inp.value.trim() || null; go.disabled = true; clearTimeout(deb); deb = setTimeout(replan, 250); });
      inp.addEventListener('keydown', e => { if (e.key === 'Enter' && !go.disabled) go.click(); });
      go.addEventListener('click', async () => {
        if (running || !plan || !plan.token) return; running = true; go.disabled = true;
        try { await D.api('/api/ctl/run', { token: plan.token }); close(); D.toast(c.label + ' · ' + ACT[action] + (plan.dry ? ' (dry-run)' : '') + ' 시작'); poll(); }
        catch (e) { running = false; await replan(); no.insertBefore(el('div', String(e.message || e)), no.firstChild); }
      });
      replan();
    });
  }

  // ---------------------------------------------------------------- the tab
  function lamp(l) { return el('span.lamp.' + (l.up === true ? 'up' : l.up === false ? 'down' : 'unk'), el('i'), l.label + (l.up === true ? '' : l.up === false ? ' · 꺼짐' : ' · 모름')); }

  function compRow(c, busy) {
    const btns = el('div.ctlbtns');
    for (const a of c.actions) {
      const blocked = a.blocked.length > 0;
      btns.appendChild(el('button.btn.sm' + (a.id === 'on' ? '.primary' : a.id === 'off' ? '.danger' : '.ghost') + (blocked ? '.blocked' : ''),
        { disabled: busy, title: blocked ? a.blocked.join('\n') : a.commands.join('\n'), onclick: () => ask(c.id, a.id) }, ACT[a.id]));
    }
    return el('div.ctlrow', el('div.ctlnm', el('b', c.label), el('div.lamps', c.lamps.map(lamp))), el('div.ctldep', c.dep), btns);
  }

  function jobBox(j, running) {
    const head = el('div.ctljh', el('b', nameOf(j.comp) + ' · ' + ACT[j.action] + (j.arg ? ' ' + j.arg : '')),
      running ? D.tag('실행 중 · ' + j.step + '/' + j.commands.length + ' 단계', 'blue') : rcTag(j),
      el('span.s', hms(j.t0) + (j.t1 ? ' → ' + hms(j.t1) : '') + ' · ' + j.client + ' · runs/…/' + j.log));
    C.tail = el('pre.ctlout', j.tail || '');
    return el('div.ctljob', head, j.note ? el('div.err', j.note) : null, C.tail);
  }

  function draw() {
    if (!C.root) return;
    const st = C.st, b = C.body;
    if (!st) { b.textContent = ''; b.appendChild(el('div.empty', C.err ? '상태를 읽지 못했습니다: ' + C.err : '상태를 읽는 중…')); C.sig = ''; return; }
    if (!st.on) { b.textContent = ''; b.appendChild(el('div.empty', '제어 기능이 꺼져 있습니다 (.env DASH_CTL=0).')); C.sig = ''; return; }
    const j = st.busy || st.last;
    const sig = JSON.stringify([st.comps, st.mode, st.run, st.rec, st.dry, st.can_sudo, !!st.busy, j && [j.id, j.step, j.rc, j.note], st.hist.length, C.err]);
    if (sig !== C.sig) {
      C.sig = sig; const y = window.scrollY; b.textContent = '';
      if (st.dry) b.appendChild(el('div.ctlban', 'DRY-RUN 모드: 버튼은 명령을 실행하지 않고, 실행했을 명령줄만 기록합니다 (.env DASH_CTL_DRYRUN).'));
      if (C.err) b.appendChild(el('div.ctlban.bad', '상태 갱신 실패: ' + C.err));
      const top = D.card('s12', '스택 상태', '버튼은 ' + st.root + ' 의 운영 스크립트를 그대로 실행합니다');
      top.add(el('div.chips',
        el('span.chip', 'GPU 모드', el('b', st.mode === 'baseline' ? 'Baseline (MIG 4g + 3g)' : st.mode === 'ours' ? 'Ours (MIG 꺼짐, 132 SM)' : String(st.mode || '–'))),
        el('span.chip', '현재 run', el('b', st.run || '없음')),
        el('span.chip', '트래픽 생성기 스위치', el('b', st.tgen && st.tgen.on ? 'On' + (st.tgen.run ? ' · 실행 중 · ' + (st.tgen.load || '?') : ' · 미실행') : 'Off')),
        el('span.chip', '측정', el('b', st.rec || '없음'))));
      top.add(el('div.note', 'GPU 모드 전환(MIG)은 root 권한이 필요해 이 화면에서 제공하지 않습니다. 터미널: ./all.sh down → ./all.sh mode <ours|baseline> → sudo scripts/mig.sh <off|on> → ./all.sh up <이름>.'));
      b.appendChild(el('div.grid', top.el));
      const cc = D.card('s12', '컴포넌트', '누르면 실행할 명령과 주의 사항을 먼저 보여 줍니다 · 흐린 버튼은 지금 실행할 수 없는 동작 (눌러서 이유 확인)');
      for (const c of st.comps) cc.add(compRow(c, !!st.busy));
      b.appendChild(el('div.grid', cc.el));
      const jc = D.card('s12', st.busy ? '실행 중인 동작' : '마지막 동작', '스크립트의 출력과 종료 코드 그대로');
      jc.add(j ? jobBox(j, !!st.busy) : el('div.empty', '이 대시보드에서 실행한 동작이 아직 없습니다.'));
      b.appendChild(el('div.grid', jc.el));
      const hc = D.card('s12', '동작 기록', st.log);
      if (!st.hist.length) hc.add(el('div.empty', '기록 없음 (대시보드가 다시 뜬 뒤의 동작만 여기 보입니다. 전체는 위 파일).'));
      else hc.add(el('table.tbl', el('thead', el('tr', ['시각', '컴포넌트', '동작', '명령', '종료 코드', '요청한 PC'].map(h => el('th', h)))),
        el('tbody', st.hist.slice().reverse().map(h => el('tr', el('td', hms(h.t0)), el('td', nameOf(h.comp)), el('td', ACT[h.action] || h.action),
          el('td.mono', h.commands.join(' ; ')), el('td', rcTag(h)), el('td', h.client || '–'))))));
      b.appendChild(el('div.grid', hc.el));
      window.scrollTo(0, y);
    } else if (j && C.tail && C.tail.textContent !== (j.tail || '')) {
      const stick = C.tail.scrollTop + C.tail.clientHeight >= C.tail.scrollHeight - 8;
      C.tail.textContent = j.tail || ''; if (stick) C.tail.scrollTop = C.tail.scrollHeight;
    }
  }

  D.views.ctl = {
    build(host) {            // the root outlives a rebuild of the tabs (live <-> Replay): the control tab always shows the live stack
      if (!C.root) { C.body = el('div.ctlbody'); C.root = el('div.ctl', C.body); draw(); }
      host.appendChild(C.root); shown(S.tab === 'ctl');
    },
    update() {},
  };
  D.ctl = { shown };
})();
