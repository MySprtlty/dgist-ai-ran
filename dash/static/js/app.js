/* _dgist experiment dashboard - page logic (2026-11-12): live feed (Server-Sent Events), measurement start / stop, replay of a recorded file.
   The views draw D.frame(): the live feed, or a replay cut at its playback position - the same code path for both. */
'use strict';
(function () {
  const D = window.D, el = D.el, $ = D.$, $$ = D.$$, S = D.S, f = D.f;
  const TABS = ['ov', 'va', 'nv', 'gpu', 'radio', 'rec'], built = {};
  let shownSummary = null, timer = null, wasOffline = false, chipSig = '';
  const NS = 'http://www.w3.org/2000/svg';
  const icon = d => { const s = document.createElementNS(NS, 'svg'), p = document.createElementNS(NS, 'path'); s.setAttribute('viewBox', '0 0 16 16'); p.setAttribute('d', d); p.setAttribute('fill', 'currentColor'); s.appendChild(p); return s; };
  const PLAY = 'M4 2.5v11l9-5.5z', PAUSE = 'M3.5 2.5h3v11h-3zM9.5 2.5h3v11h-3z';

  // ------------------------------------------------------------------ drawing
  function ensure() {
    const t = S.tab, m = S.rp ? S.rp.meta : S.meta; if (built[t] || !m) return;
    D.views[t].build($('#tab-' + t), m); built[t] = true;
  }
  function render() {
    const F = D.frame(); D.recNow = !!(F.cur && F.cur.rec);
    ensure(); if (built[S.tab]) D.views[S.tab].update(F);
    header(F);
  }
  function renderScope() { const v = D.views[S.tab]; if (built[S.tab] && v.scope) v.scope(D.frame()); }
  function rebuild() {          // the source changed (live <-> replay, or new meta): build the views again
    for (const c of D.charts) c.destroy(); D.charts.length = 0;
    for (const t of TABS) { $('#tab-' + t).textContent = ''; built[t] = false; }
    render();
  }
  function setTab(t) {
    if (TABS.indexOf(t) < 0) return; S.tab = t;
    for (const b of $$('#tabs .tabbtn')) b.classList.toggle('on', b.dataset.tab === t);
    for (const x of TABS) $('#tab-' + x).classList.toggle('on', x === t);
    window.scrollTo(0, 0); render();
  }
  function seg(id, key, cb) {
    $(id).addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      for (const x of $$('button', $(id))) x.classList.toggle('on', x === b);
      if (key) S[key] = b.dataset.v; cb && cb(b.dataset.v);
    });
  }

  // ------------------------------------------------------------------ header: mode, components, measurement state
  const CHIPS = [['l1', 'L1'], ['gnb', 'gNB'], ['yolo', 'YOLO 서버'], ['core', '5G 코어'], ['va', 'VA'], ['nonva', 'Non-VA'], ['xapp', 'xApp'], ['vcam', '가상 카메라']];
  function header(F) {
    const m = F.meta || {}, t = F.cur, sys = (t && t.sys) || {}, mode = sys.mode || m.mode, mb = $('#modeBadge');
    D.txt(mb, !S.meta && !S.rp ? '연결 중' : mode === 'baseline' ? 'Baseline · MIG 고정 분할' : 'Ours · Green Context'); D.cls(mb, 'badge ' + (mode === 'baseline' ? 'base' : 'ours'));
    $('#simBadge').hidden = !m.sim;
    const up = sys.up || {}, list = CHIPS.filter(c => c[0] === 'yolo' ? mode === 'baseline' : c[0] === 'xapp' ? mode !== 'baseline' : c[0] === 'vcam' ? !!up.vcam : true);
    const sig = list.map(c => c[0] + (up[c[0]] ? 1 : 0)).join();
    if (sig !== chipSig) { chipSig = sig; const h = $('#sysChips'); h.textContent = ''; for (const c of list) h.appendChild(el('span.sysc' + (up[c[0]] ? '.on' : ''), { title: c[1] + (up[c[0]] ? ' 동작 중' : ' 꺼짐') }, el('i'), c[1])); }
    recState();
  }
  function recState() {        // always the LIVE state, also while a replay is on screen
    const r = S.meta && S.meta.rec, L = S.live.ticks, last = L[L.length - 1], b = $('#recBtn');
    $('#recInd').hidden = !r;
    if (r) { D.txt($('#recName'), r.name); D.txt($('#recTime'), D.mmss(last && last.rec ? last.el : Date.now() / 1000 - r.t0)); }
    D.txt(b, r ? '측정 종료' : '측정 시작'); D.cls(b, 'btn ' + (r ? 'danger' : 'primary'));
  }

  // ------------------------------------------------------------------ modals
  function modal(wide, build) {
    const host = $('#modalHost'); host.textContent = '';
    const m = el('div.modal' + (wide ? '.wide' : '')), bd = el('div.backdrop', m);
    const esc = e => { if (e.key === 'Escape') close(); };
    function close() { bd.remove(); document.removeEventListener('keydown', esc); }
    bd.addEventListener('mousedown', e => { if (e.target === bd) close(); });
    document.addEventListener('keydown', esc);
    build(m, close); host.appendChild(bd); return close;
  }
  const modalOpen = () => !!$('#modalHost').firstChild;

  function startModal() {
    const L = S.live.ticks, t = L[L.length - 1], sys = (t && t.sys) || {}, up = sys.up || {}, src = sys.src || {}, mode = sys.mode || (S.meta || {}).mode;
    const now = new Date(), p2 = n => String(n).padStart(2, '0');
    const sug = (mode === 'baseline' ? 'baseline' : 'ours') + '_' + p2(now.getMonth() + 1) + p2(now.getDate()) + '_' + p2(now.getHours()) + p2(now.getMinutes());
    modal(false, (m, close) => {
      const inp = el('input.field', { type: 'text', maxlength: '80', placeholder: '예: ours_4ue_q091_1차', value: sug, spellcheck: 'false' }), err = el('div.err');
      const go = el('button.btn.primary', '측정 시작');
      const checks = [['VA 서비스', up.va], ['Non-VA 서비스', up.nonva], ['L1', up.l1], ['gNB', up.gnb], ['dApp 에이전트 (추정 · SM)', src.agent], [mode === 'baseline' ? '슬롯 CSV (Baseline은 없음)' : '슬롯 CSV (슬롯별 SM)', src.slots],
        [mode === 'baseline' ? 'xApp (Baseline은 없음)' : 'xApp (PRB 슬라이싱)', up.xapp]];
      for (const u of D.UE) { const x = t && t.ue[u.k]; checks.push([u.name + (x && x.on ? ' 전송 중' : ' 전송 없음'), x && x.on]); }
      const submit = async () => {
        const name = inp.value.trim(); if (!name) { D.txt(err, '실험 이름을 입력하세요.'); inp.focus(); return; }
        go.disabled = true; D.txt(err, '');
        try { const r = await D.api('/api/rec/start', { name }); onMsg(r.meta); close(); D.toast('측정 시작: ' + name); }
        catch (e) { D.txt(err, e.message); go.disabled = false; }
      };
      inp.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
      go.addEventListener('click', submit);
      m.appendChild(el('h2', '측정 시작'));
      m.appendChild(el('div.desc', '실험 이름을 정하면 그 이름으로 폴더와 파일을 만들어 모든 지표를 기록합니다. 화면의 누적 평균은 이 순간부터 다시 셉니다.'));
      m.appendChild(inp);
      m.appendChild(el('div.checks', checks.map(c => el('div' + (c[1] ? '.ok' : ''), el('i'), c[0]))));
      m.appendChild(el('div.note', '회색 항목은 지금 데이터가 들어오지 않는 것입니다. 그대로 시작할 수 있지만 그 지표는 비어 있게 됩니다. 디스크 여유 ' + f(sys.disk_gb != null ? sys.disk_gb : S.info.free_gb, 0) + ' GB (기록은 1시간에 약 0.2 GB).'));
      m.appendChild(err);
      m.appendChild(el('div.acts', el('button.btn.ghost', { onclick: close }, '취소'), go));
      setTimeout(() => { inp.focus(); inp.select(); }, 30);
    });
  }
  function stopModal() {
    const r = S.meta && S.meta.rec; if (!r) return;
    modal(false, (m, close) => {
      const L = S.live.ticks, last = L[L.length - 1], err = el('div.err'), go = el('button.btn.danger', '측정 종료');
      go.addEventListener('click', async () => {
        go.disabled = true;
        try { const d = await D.api('/api/rec/stop', {}); close(); summaryModal(d.summary); }
        catch (e) { D.txt(err, e.message); go.disabled = false; }
      });
      m.appendChild(el('h2', '측정을 종료할까요?'));
      m.appendChild(el('div.desc', '“' + r.name + '” · ' + D.mmss(last && last.rec ? last.el : 0) + ' 기록됨. 종료하면 결과를 요약하고 Replay 파일을 저장합니다.'));
      m.appendChild(err);
      m.appendChild(el('div.acts', el('button.btn.ghost', { onclick: close }, '계속 측정'), go));
    });
  }
  function summaryModal(s, fromReplay) {
    if (!s) { D.toast('요약 없음 (비정상 종료된 기록)'); return; }
    if (!fromReplay) { if (shownSummary === s.id) return; shownSummary = s.id; }
    modal(true, (m, close) => {
      m.appendChild(el('h2', '측정 결과 요약 · ' + (s.name || '')));
      m.appendChild(el('div.desc', '측정 시작부터 종료까지의 누적 평균입니다. 같은 내용이 summary.json / summary.txt 로 저장되었습니다.'));
      m.appendChild(D.summaryView(s, fromReplay && !(S.rp && S.rp.id) ? null : s.id));
      m.appendChild(el('div.acts', fromReplay ? null : el('button.btn.ghost', { onclick: () => { close(); D.act.replayServer(s.id, s.name); } }, '▶ Replay로 다시 보기'), el('button.btn.primary', { onclick: close }, '닫기')));
    });
  }

  // ------------------------------------------------------------------ live feed
  function onMsg(m) {
    if (!m || !m.k) return;
    if (m.k === 'tick') {
      let L = S.live.ticks; const last = L[L.length - 1];
      if (last && (!!last.rec !== !!m.rec || m.t <= last.t)) L = S.live.ticks = [];       // a measurement started or ended: the charts start over
      L.push(m);
      if (!m.rec) { if (L.length > 600) L.splice(0, L.length - 600); }
      else if (L.length > 7200) { const h = L.length >> 1, keep = []; for (let i = 0; i < h; i += 2) keep.push(L[i]); S.live.ticks = keep.concat(L.slice(h)); }   // long measurements: thin the older half
      if (!S.rp) render(); else recState();
    } else if (m.k === 'scope') { S.live.scope = m; if (!S.rp) renderScope(); }
    else if (m.k === 'meta') {
      const o = S.meta, same = o && o.mode === m.mode && String(o.models) === String(m.models) && String(o.classes) === String(m.classes) && o.base_model === m.base_model && o.sim === m.sim;
      S.meta = m; if (!S.rp) { if (same) render(); else rebuild(); } else recState();
    } else if (m.k === 'end') {
      if (m.summary) summaryModal(m.summary);
      if (built.rec) D.views.rec.refresh();
    }
  }
  async function init() {
    let d; try { d = await D.api('/api/init'); } catch (e) { $('#offline').hidden = false; wasOffline = true; setTimeout(init, 2000); return false; }
    S.meta = d.meta; S.live.ticks = d.ticks || []; S.live.scope = d.scope || null; S.info = { free_gb: d.free_gb, min_free_gb: d.min_free_gb };
    if (!S.rp) rebuild(); else recState();
    return true;
  }
  function connect() {
    const es = new EventSource('/api/stream');
    es.onopen = () => { S.online = true; $('#offline').hidden = true; if (wasOffline) { wasOffline = false; init(); } };
    es.onerror = () => { S.online = false; wasOffline = true; $('#offline').hidden = false; };
    es.onmessage = e => { let m; try { m = JSON.parse(e.data); } catch (x) { return; } onMsg(m); };
  }

  // ------------------------------------------------------------------ replay
  const SCOPE_RE = /^\{"k":"scope","t":([0-9.]+)/;
  function parseReplay(text, name, id) {
    const rp = { name, id: id || null, meta: null, ticks: [], scopes: [], summary: null, ended: false, pos: 0, playing: true, speed: 1, ti: -1, si: -1, sc: null };
    let a = 0, bad = 0; const n = text.length;
    while (a < n) {
      let b = text.indexOf('\n', a); if (b < 0) b = n;
      if (b > a) {
        const line = text.slice(a, b), sm = SCOPE_RE.exec(line);
        if (sm) rp.scopes.push({ t: Number(sm[1]), raw: line });           // scope frames are parsed when shown (most of the file)
        else {
          try {
            const o = JSON.parse(line);
            if (o.k === 'tick') rp.ticks.push(o); else if (o.k === 'meta') rp.meta = o; else if (o.k === 'end') { rp.summary = o.summary || null; rp.ended = true; }
          } catch (e) { bad++; }                                            // a cut last line of an unfinished file
        }
      }
      a = b + 1;
    }
    if (!rp.meta || !rp.ticks.length) throw new Error('Replay 파일이 아닙니다 (meta · tick 줄이 없음)');
    for (const t of rp.ticks) t.rec = true;
    const t0 = rp.ticks[0];
    rp.t0 = (rp.meta.rec && rp.meta.rec.t0) || (t0.t - (t0.el || 1)); rp.dur = Math.max(1, rp.ticks[rp.ticks.length - 1].t - rp.t0); rp.pos = Math.min(rp.dur, Math.max(1, t0.t - rp.t0));
    if (!rp.name) rp.name = (rp.meta.rec && rp.meta.rec.name) || 'replay';
    return rp;
  }
  const lastLE = (arr, t) => { let a = 0, b = arr.length; while (a < b) { const m = (a + b) >> 1; if (arr[m].t <= t) a = m + 1; else b = m; } return a - 1; };
  function rpLocate() {         // -> 2 when the tick changed, 1 when only the scope frame changed
    const rp = S.rp, t = rp.t0 + rp.pos + 1e-6, ti = lastLE(rp.ticks, t), si = lastLE(rp.scopes, t);
    let ch = 0;
    if (si !== rp.si) { rp.si = si; rp.sc = null; if (si >= 0) { try { rp.sc = JSON.parse(rp.scopes[si].raw); } catch (e) { rp.sc = null; } } ch = 1; }
    if (ti !== rp.ti) { rp.ti = ti; ch = 2; }
    return ch;
  }
  function rpBar() {
    const rp = S.rp; if (!rp) return;
    D.txt($('#rpTime'), D.mmss(rp.pos) + ' / ' + D.mmss(rp.dur));
    if (document.activeElement !== $('#rpSeek') || rp.playing) $('#rpSeek').value = String(100 * rp.pos / rp.dur);
    const b = $('#rpPlay'), want = rp.playing ? 'pause' : 'play';
    if (b.dataset.i !== want) { b.dataset.i = want; b.textContent = ''; b.appendChild(icon(rp.playing ? PAUSE : PLAY)); }
  }
  function rpStep(force) { const ch = rpLocate(); if (ch === 2 || force) render(); else if (ch === 1) renderScope(); rpBar(); }
  function rpTick() {
    const rp = S.rp; if (!rp || !rp.playing) return;
    rp.pos += 0.2 * rp.speed; if (rp.pos >= rp.dur) { rp.pos = rp.dur; rp.playing = false; }
    rpStep(false);
  }
  function startReplay(rp) {
    S.rp = rp; $('#replayBar').hidden = false; document.documentElement.style.setProperty('--rb', '52px'); D.txt($('#rpName'), rp.name + (rp.ended ? '' : ' (비정상 종료된 기록)'));
    for (const x of $$('#rpSpeed button')) x.classList.toggle('on', x.dataset.v === '1');
    rpLocate(); S.tab = 'ov'; for (const b of $$('#tabs .tabbtn')) b.classList.toggle('on', b.dataset.tab === 'ov'); for (const x of TABS) $('#tab-' + x).classList.toggle('on', x === 'ov');
    rebuild(); rpBar(); window.scrollTo(0, 0);
    if (!timer) timer = setInterval(rpTick, 200);
    D.toast('Replay: ' + rp.name + ' · ' + D.mmss(rp.dur) + ' · ' + f(rp.ticks.length, 0) + ' 초 분량');
  }
  function stopReplay() {
    if (!S.rp) return; S.rp = null; if (timer) { clearInterval(timer); timer = null; }
    $('#replayBar').hidden = true; document.documentElement.style.removeProperty('--rb'); rebuild();
  }
  D.act = {
    async replayServer(id, name) {
      D.toast('기록을 읽는 중…', 1500);
      try {
        const r = await fetch('/api/recs/' + encodeURIComponent(id) + '/replay'); if (!r.ok) throw new Error('HTTP ' + r.status);
        startReplay(parseReplay(await r.text(), name, id));
      } catch (e) { D.toast('Replay를 열지 못했습니다: ' + e.message, 5000); }
    },
    async replayFile(file) {
      D.toast('파일을 읽는 중… (' + D.bytes(file.size) + ')', 1500);
      try { startReplay(parseReplay(await file.text(), null, null)); } catch (e) { D.toast('Replay를 열지 못했습니다: ' + e.message, 5000); }
    },
    async openSummary(id, name) {
      try { const s = await D.api('/api/recs/' + encodeURIComponent(id) + '/summary'); D.views.rec.showSummary(s, id, name); D.views.rec.p.sumGrid.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
      catch (e) { D.toast('요약을 읽지 못했습니다: ' + e.message, 5000); }
    },
  };

  // ------------------------------------------------------------------ controls
  function theme(set) {
    const root = document.documentElement;
    if (set) { root.dataset.theme = set; try { localStorage.setItem('dash.theme', set); } catch (e) { /* private window */ } }
    D.themeChanged();
  }
  document.addEventListener('DOMContentLoaded', () => {
    try { const t = localStorage.getItem('dash.theme'); if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t; } catch (e) { /* private window */ }
    $('#tabs').addEventListener('click', e => { const b = e.target.closest('.tabbtn'); if (b) setTab(b.dataset.tab); });
    seg('#segVal', 'val', render); seg('#segWin', 'win', render);
    $('#recBtn').addEventListener('click', () => { if (S.meta && S.meta.rec) stopModal(); else if (S.meta) startModal(); });
    $('#themeBtn').addEventListener('click', () => {
      const cur = document.documentElement.dataset.theme || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); theme(cur === 'dark' ? 'light' : 'dark');
    });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => theme());
    $('#fsBtn').addEventListener('click', () => { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => { }); });
    $('#rpPlay').addEventListener('click', () => { const rp = S.rp; if (!rp) return; if (!rp.playing && rp.pos >= rp.dur) rp.pos = Math.min(rp.dur, 1); rp.playing = !rp.playing; rpStep(false); });
    $('#rpSeek').addEventListener('input', e => { const rp = S.rp; if (!rp) return; rp.pos = rp.dur * Number(e.target.value) / 100; rpStep(false); });
    seg('#rpSpeed', null, v => { if (S.rp) S.rp.speed = Number(v); });
    $('#rpSummary').addEventListener('click', () => { if (S.rp) summaryModal(S.rp.summary, true); });
    $('#rpClose').addEventListener('click', stopReplay);
    document.addEventListener('keydown', e => {
      if (modalOpen() || e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if (e.key >= '1' && e.key <= '6') setTab(TABS[Number(e.key) - 1]);
      else if (e.key === ' ' && S.rp) { e.preventDefault(); $('#rpPlay').click(); }
    });
    setInterval(() => { if (S.meta && S.meta.rec) recState(); }, 1000);
    header(D.frame());
    init().then(() => connect());
  });
})();
