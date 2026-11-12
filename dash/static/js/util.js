/* _dgist experiment dashboard - helpers, shared state (2026-11-12). Plain scripts, one global: D. */
'use strict';
window.D = (function () {
  const D = { charts: [] };

  // ---------------------------------------------------------------- DOM
  D.$ = (s, r) => (r || document).querySelector(s);
  D.$$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  function add(e, k) {
    if (k == null || k === false) return;
    if (Array.isArray(k)) { for (const x of k) add(e, x); return; }
    e.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  // D.el('div.card.s4', {style: '...', onclick: fn}, child, 'text', [children])
  D.el = function (spec, a) {
    const m = /^([a-z0-9]*)((?:\.[\w-]+)*)$/i.exec(spec), e = document.createElement(m[1] || 'div');
    if (m[2]) e.className = m[2].slice(1).split('.').join(' ');
    let i = 1;
    if (a && typeof a === 'object' && !(a instanceof Node) && !Array.isArray(a)) {
      i = 2;
      for (const k in a) {
        const v = a[k];
        if (v == null || v === false) continue;
        if (k.slice(0, 2) === 'on' && typeof v === 'function') e.addEventListener(k.slice(2), v);
        else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
        else if (k === 'text') e.textContent = v;
        else e.setAttribute(k, v === true ? '' : v);
      }
    }
    for (; i < arguments.length; i++) add(e, arguments[i]);
    return e;
  };
  // D.fill(el, ['now ', ['b', '12.3 ms'], ' ...'])  - text and <b>/<small> runs, never innerHTML
  D.fill = function (e, parts) {
    const key = JSON.stringify(parts); if (e._k === key) return; e._k = key;
    e.textContent = '';
    for (const p of [].concat(parts)) {
      if (p == null || p === false) continue;
      if (Array.isArray(p)) { const x = document.createElement(p[0]); x.textContent = p[1]; if (p[2]) x.className = p[2]; e.appendChild(x); }
      else e.appendChild(document.createTextNode(String(p)));
    }
  };
  D.txt = function (e, s) { if (e && e._t !== s) { e._t = s; e.textContent = s; } };
  D.cls = function (e, c) { if (e._c !== c) { e._c = c; e.className = c; } };

  // ---------------------------------------------------------------- numbers, time
  D.num = v => typeof v === 'number' && isFinite(v);
  D.f = function (v, d) {
    if (!D.num(v)) return '–';
    const s = v.toFixed(d == null ? 1 : d), i = s.indexOf('.'), a = i < 0 ? s : s.slice(0, i);
    return a.length > (v < 0 ? 4 : 3) ? a.replace(/\B(?=(\d{3})+$)/g, ',') + (i < 0 ? '' : s.slice(i)) : s;
  };
  D.fu = (v, d, u) => D.num(v) ? D.f(v, d) + (u ? ' ' + u : '') : '–';
  D.pct = (a, b) => (D.num(a) && D.num(b) && b > 0) ? 100 * a / b : null;
  const p2 = n => (n < 10 ? '0' : '') + n;
  D.mmss = function (s) { s = Math.max(0, Math.round(s || 0)); const h = (s / 3600) | 0, m = ((s % 3600) / 60) | 0; return (h ? h + ':' : '') + p2(m) + ':' + p2(s % 60); };
  D.clock = function (t, sec) { const d = new Date(t * 1000); return p2(d.getHours()) + ':' + p2(d.getMinutes()) + (sec === false ? '' : ':' + p2(d.getSeconds())); };
  D.g = function (o, p) { const ks = p.split('.'); for (let i = 0; i < ks.length; i++) { if (o == null) return undefined; o = o[ks[i]]; } return o; };
  D.bytes = function (n) { return !D.num(n) ? '–' : n >= 1e9 ? D.f(n / 1e9, 2) + ' GB' : n >= 1e6 ? D.f(n / 1e6, 1) + ' MB' : n >= 1e3 ? D.f(n / 1e3, 0) + ' KB' : n + ' B'; };
  D.sum = o => { let s = 0; for (const k in (o || {})) s += o[k] || 0; return s; };

  // a number that glides to its new value
  D.setNum = function (e, v, d) {
    if (!D.num(v)) { e._v = null; e._tw = (e._tw || 0) + 1; D.txt(e, '–'); return; }
    const from = D.num(e._v) ? e._v : v; e._v = v;
    if (from === v || document.hidden || D.noAnim) { e._t = null; e.textContent = D.f(v, d); return; }
    const t0 = performance.now(), id = (e._tw = (e._tw || 0) + 1);
    const step = now => {
      if (e._tw !== id) return;
      const k = Math.min(1, (now - t0) / 320), q = 1 - Math.pow(1 - k, 3);
      e._t = null; e.textContent = D.f(from + (v - from) * q, d);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  // ---------------------------------------------------------------- theme colours for canvas drawing
  const cvc = {};
  D.cv = n => cvc[n] || (cvc[n] = getComputedStyle(document.documentElement).getPropertyValue(n).trim());
  D.themeChanged = function () { for (const k in cvc) delete cvc[k]; for (const c of D.charts) c.redraw(); };
  D.FONT = "'Pretendard Variable', Pretendard, 'Noto Sans CJK KR', 'Noto Sans KR', sans-serif";

  // ---------------------------------------------------------------- the entities and their fixed colours
  D.UE = [
    { k: 'va1', svc: 'va', name: 'VA UE1', short: 'UE1', color: '--c1' },
    { k: 'va2', svc: 'va', name: 'VA UE2', short: 'UE2', color: '--c2' },
    { k: 'nv1', svc: 'nv', name: 'Non-VA UE1', short: 'UE1', color: '--c3' },
    { k: 'nv2', svc: 'nv', name: 'Non-VA UE2', short: 'UE2', color: '--c4' },
  ];
  D.SVC = { va: { name: 'VA 서비스', color: '--c1', weak: '--c1w' }, nv: { name: 'Non-VA 서비스', color: '--c3', weak: '--c3w' } };
  // latency parts of one VA frame (colour = the part, the UE is named by the card)
  D.LAT = {
    ul: { label: 'Uplink', color: '--c3' }, wait: { label: '대기', color: '--c4' }, inf: { label: 'GPU 추론', color: '--c1' },
    srv: { label: '서버 처리', color: '--order' }, dl: { label: 'Downlink', color: '--c2' }, e2e: { label: 'E2E', color: '--total' }, ai: { label: 'Inference', color: '--c1' },
  };
  D.VERDICT = { ok: ['여유', 'good', '●'], warn: ['주의', 'warn', '▲'], sat: ['포화', 'bad', '■'], idle: ['대기', '', '○'] };

  // ---------------------------------------------------------------- shared state
  D.S = {
    meta: null,                       // the live meta
    live: { ticks: [], scope: null }, // the live feed (kept while a replay is shown)
    rp: null,                         // replay: {name, id, meta, ticks, scopes, summary, t0, dur, pos, playing, speed, ti, si}
    val: 'both', win: 'all', tab: 'ov', online: false, info: {},
  };
  D.recNow = false;                   // the frame on screen belongs to a measurement (live recording or replay)
  // the cumulative mean while a measurement runs, else the live value
  D.pk = (x, k) => !x ? null : (D.recNow && x.c) ? x.c[k] : x[k];
  // what the views draw: the live feed or the replay cut at its playback position
  D.frame = function () {
    const S = D.S, rp = S.rp;
    let ticks, n, scope, meta;
    if (rp) { ticks = rp.ticks; n = rp.ti + 1; scope = rp.sc || null; meta = rp.meta; }
    else { ticks = S.live.ticks; n = ticks.length; scope = S.live.scope; meta = S.meta; }
    const cur = n > 0 ? ticks[n - 1] : null;
    let i0 = 0;
    if (cur && S.win !== 'all') {
      const lim = cur.t - Number(S.win); let a = 0, b = n - 1;
      while (a < b) { const m = (a + b) >> 1; if (ticks[m].t < lim) a = m + 1; else b = m; }
      i0 = a;
    }
    return { ticks, n, i0, cur, scope, meta: meta || {}, replay: !!rp };
  };

  // ---------------------------------------------------------------- small components
  D.tag = function (text, kind) { return D.el('span.tag' + (kind ? '.' + kind : ''), text); };
  // KPI tile: label (+ "cumulative"/"live" tag), big number + unit, one line below
  D.tile = function (label, cls) {
    const kind = D.el('span.tag'), lb = D.el('div.lb', D.el('span', label), kind), v = D.el('span'), u = D.el('span.u'), sub = D.el('div.sub');
    const e = D.el('div.tile' + (cls ? '.' + cls : ''), lb, D.el('div.big', v, u), sub);
    kind.hidden = true;
    const T = {
      el: e, sub, lb,
      set(val, d, unit) { D.setNum(v, val, d); D.txt(u, unit || ''); return T; },
      text(s, unit) { v._v = null; v._tw = (v._tw || 0) + 1; D.txt(v, s); D.txt(u, unit || ''); return T; },
      kind(cum) { kind.hidden = cum == null; if (cum != null) { D.txt(kind, cum ? '누적 평균' : '실시간'); D.cls(kind, 'tag' + (cum ? ' blue' : '')); } return T; },
      // cumulative mean in front when a measurement runs, else the live value
      kpi(cum, now, d, unit, extra) {
        const has = D.recNow; T.set(has ? cum : now, d, unit).kind(has);
        D.fill(sub, (has ? ['지금 ', ['b', D.fu(now, d, unit)]] : ['측정 시작 후 누적 평균 표시']).concat(extra && extra.length ? [' · '].concat(extra) : []));
        return T;
      },
    };
    return T;
  };
  D.card = function (span, title, sub) {
    const r = D.el('div.r'), h = D.el('div.cardh', title ? D.el('span.t', title) : null, sub != null ? D.el('span.s', sub) : null, r);
    const e = D.el('div.card' + (span ? '.' + span : ''), h);
    return { el: e, head: h, right: r, subEl: h.querySelector('.s'), add() { for (const x of arguments) add(e, x); return this; } };
  };
  D.kv = function (rows, cls) {     // rows: [[label, key]] -> {el, set(key, text, small)}
    const e = D.el('div.kv' + (cls ? '.' + cls : '')), m = {};
    for (const [lab, key] of rows) { const b = D.el('b'); m[key] = b; e.appendChild(D.el('span', lab)); e.appendChild(b); }
    return { el: e, set(key, text, small) { D.fill(m[key], small ? [text, ['small', small]] : [text]); } };
  };
  D.legendItem = function (color, label, kind) {
    const i = D.el('i' + (kind === 'line' ? '.ln' : kind === 'dash' ? '.dash' : '')), b = D.el('b'), u = D.el('span.u');
    if (kind !== 'dash') i.style.background = color.slice(0, 2) === '--' ? 'var(' + color + ')' : color;
    const e = D.el('span.lg', i, D.el('span', label), b, u);
    return { el: e, set(v, unit) { D.txt(b, v == null ? '' : v); D.txt(u, unit || ''); } };
  };
  D.toast = function (msg, ms) {
    const t = D.el('div.toast', msg); document.body.appendChild(t); setTimeout(() => t.remove(), ms || 3200);
  };
  D.api = async function (path, body) {
    const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let j = null; try { j = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error((j && (j.detail || j.error)) || ('HTTP ' + r.status));
    return j;
  };
  return D;
})();
