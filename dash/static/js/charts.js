/* _dgist experiment dashboard - canvas charts (2026-11-12): time chart (lines or stacked area), slot scope.
   Rules kept here: one y axis per chart, cumulative mean = bold line, live value = thin line of the same hue, text in ink colours,
   2 px lines with round joins, end markers with a surface ring, hairline grid, crosshair + one tooltip. */
'use strict';
(function () {
  const D = window.D;
  const dpr = () => Math.max(1, Math.min(3, window.devicePixelRatio || 1));
  const font = (px, w) => (w || 500) + ' ' + px + 'px ' + D.FONT;

  function nice(max, nt) {
    if (!(max > 0)) max = 1;
    const raw = max / (nt || 4), p = Math.pow(10, Math.floor(Math.log10(raw))), m = raw / p;
    const st = (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
    return { step: st, max: Math.ceil(max / st - 1e-9) * st };
  }
  const TSTEPS = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];

  class Base {
    constructor(host) {
      this.host = host; host.classList.add('chart');
      this.cvs = document.createElement('canvas'); host.appendChild(this.cvs);
      this.tip = D.el('div.tip'); host.appendChild(this.tip);
      this.ctx = this.cvs.getContext('2d'); this.w = 0; this.h = 0; this.d = 1; this.mx = null; this.my = null; this.F = null;
      this.ro = new ResizeObserver(() => { if (this.size()) this.redraw(); }); this.ro.observe(host);
      this.cvs.addEventListener('mousemove', e => { const r = this.cvs.getBoundingClientRect(); this.mx = e.clientX - r.left; this.my = e.clientY - r.top; this.redraw(); });
      this.cvs.addEventListener('mouseleave', () => { this.mx = null; this.tip.style.display = 'none'; this.redraw(); });
      D.charts.push(this);
    }
    size() {
      const w = Math.round(this.host.clientWidth), h = Math.round(this.host.clientHeight), d = dpr();
      if (!w || !h) return false;
      if (w === this.w && h === this.h && d === this.d) return false;
      this.w = w; this.h = h; this.d = d; this.cvs.width = Math.round(w * d); this.cvs.height = Math.round(h * d); return true;
    }
    destroy() { this.ro.disconnect(); this.F = null; }
    redraw() { if (this.F) this.draw(this.F); }
    begin() { const c = this.ctx; c.setTransform(this.d, 0, 0, this.d, 0, 0); c.clearRect(0, 0, this.w, this.h); c.globalAlpha = 1; c.setLineDash([]); return c; }
    ready() { if (!this.w) this.size(); return this.w > 0; }
    empty(c, msg) { c.fillStyle = D.cv('--ink6'); c.font = font(13, 500); c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(msg, this.w / 2, this.h / 2); }
    // tooltip: title + rows [colour, label, value]
    showTip(x, y, title, rows) {
      const t = this.tip; t.textContent = '';
      t.appendChild(D.el('div.tt', title));
      for (const r of rows) { const i = D.el('i'); if (r[0]) i.style.background = r[0]; else i.style.visibility = 'hidden'; t.appendChild(D.el('div.tr', i, D.el('span', r[1]), D.el('b', r[2]))); }
      t.style.display = 'block';
      const tw = t.offsetWidth, th = t.offsetHeight;
      let left = x + 14; if (left + tw > this.w - 4) left = x - 14 - tw; if (left < 4) left = 4;
      let top = y - th / 2; if (top < 2) top = 2; if (top + th > this.h - 2) top = Math.max(2, this.h - 2 - th);
      t.style.left = left + 'px'; t.style.top = top + 'px';
    }
  }

  /* TimeChart: opt = { series: [{label, color, inst: tick => v, cum: tick => v, dash, plain}], unit, digits, legend: element,
                        thresholds: [{get: meta => v, label}], stack, yFixed, yMin, minSpan, pad } */
  class TimeChart extends Base {
    constructor(host, opt) {
      super(host); this.opt = opt; this.lg = [];
      if (opt.legend) {
        for (const s of opt.series) { const it = D.legendItem(s.color, s.label, opt.stack ? 'box' : s.dash ? 'dash' : 'line'); this.lg.push(it); opt.legend.appendChild(it.el); }
        for (const th of (opt.thresholds || [])) { const it = D.legendItem('--bad', th.label, 'dash'); it.el.firstChild.style.borderTopColor = 'var(--bad)'; th._lg = it; opt.legend.appendChild(it.el); }
      }
    }
    draw(F) {
      this.F = F; if (!this.ready()) return;
      const c = this.begin(), o = this.opt, ticks = F.ticks, n = F.n, i0 = F.i0, dg = o.digits == null ? 1 : o.digits, unit = o.unit || '';
      const x0 = o.padL || 44, x1 = this.w - (o.stack || o.noEnd ? 12 : 58), y0 = unit ? 24 : 12, y1 = this.h - 26;
      if (!F.cur || n - i0 < 1) { this.empty(c, '데이터 수신 대기 중'); for (const it of this.lg) it.set('', ''); return; }
      const cur = F.cur, tA = ticks[i0].t, tB = Math.max(cur.t, tA + (o.minSpan || 60));
      const X = t => x0 + (t - tA) / (tB - tA) * (x1 - x0);
      const hasCum = !!cur.rec, mode = D.S.val;
      // which lines: cumulative mean bold, live value thin (or bold when it is the only one)
      const lines = [];
      if (o.stack) {
        for (const s of o.series) lines.push({ s, fn: (mode === 'cum' && hasCum && s.cum) ? s.cum : s.inst, bold: true });
      } else {
        for (const s of o.series) {
          const showCum = hasCum && s.cum && mode !== 'inst' && !s.plain, showInst = s.inst && (mode !== 'cum' || !showCum);
          if (showInst) lines.push({ s, fn: s.inst, bold: !showCum, kind: 'inst' });
          if (showCum) lines.push({ s, fn: s.cum, bold: true, kind: 'cum' });
        }
      }
      // points, thinned to the plot width (bucket means)
      const cnt = n - i0, step = Math.max(1, Math.floor(cnt / Math.max(50, (x1 - x0) / 1.5)));
      let vmax = 0;
      for (const L of lines) {
        const pts = []; let st = 0, sv = 0, k = 0, m = 0;
        for (let i = i0; i < n; i++) {
          const v = L.fn(ticks[i]); st += ticks[i].t; m++;
          if (D.num(v)) { sv += v; k++; }
          if (m === step || i === n - 1) { pts.push([st / m, k ? sv / k : null]); if (k && sv / k > vmax && !o.stack) vmax = sv / k; st = 0; sv = 0; k = 0; m = 0; }
        }
        L.pts = pts; L.last = null;
        for (let i = n - 1; i >= i0 && i > n - 6; i--) { const v = L.fn(ticks[i]); if (D.num(v)) { L.last = v; L.lastT = ticks[i].t; break; } }
      }
      if (!lines.some(L => L.pts.some(q => q[1] != null))) { this.empty(c, o.emptyMsg || '이 구간에 데이터가 없습니다'); for (const it of this.lg) it.set('–', ''); this.tip.style.display = 'none'; return; }
      if (o.stack) {
        const np = lines.length ? lines[0].pts.length : 0;
        for (let j = 0; j < np; j++) { let s = 0; for (const L of lines) s += L.pts[j][1] || 0; if (s > vmax) vmax = s; }
      }
      const ths = [];
      for (const th of (o.thresholds || [])) { const v = th.get(F.meta, cur); if (D.num(v)) { ths.push([th, v]); if (v > vmax) vmax = v * 1.04; if (th._lg) th._lg.set(D.f(v, th.digits == null ? 0 : th.digits), unit); } }
      const sc = o.yFixed ? { step: o.yStep || o.yFixed / 4, max: o.yFixed } : nice(Math.max(vmax * 1.06, o.yFloor || 0), 4);
      const Y = v => y1 - Math.min(1.02, v / sc.max) * (y1 - y0);

      // grid + y labels
      c.font = font(11.5, 500); c.textBaseline = 'middle'; c.textAlign = 'right'; c.lineWidth = 1;
      for (let v = 0; v <= sc.max + 1e-9; v += sc.step) {
        const y = Math.round(Y(v)) + 0.5;
        c.strokeStyle = D.cv(v === 0 ? '--line' : '--hair'); c.beginPath(); c.moveTo(x0, y); c.lineTo(x1, y); c.stroke();
        c.fillStyle = D.cv('--ink5'); c.fillText(D.f(v, sc.step < 1 ? (sc.step < 0.1 ? 2 : 1) : 0), x0 - 8, y);
      }
      // x labels: elapsed time of the measurement, else the clock
      const span = tB - tA; let ts = TSTEPS[TSTEPS.length - 1];
      for (const s of TSTEPS) if (span / s <= 6.5) { ts = s; break; }
      const base = hasCum ? cur.t - (cur.el || 0) : 0;
      c.textAlign = 'center'; c.textBaseline = 'top'; c.fillStyle = D.cv('--ink5');
      for (let t = Math.ceil((tA - base) / ts - 1e-6) * ts + base; t <= tB + 1e-6; t += ts) {
        const x = X(t); if (x < x0 + 14 || x > x1 - 8) { if (x < x0 - 1 || x > x1 + 1) continue; }
        c.fillText(hasCum ? D.mmss(t - base) : D.clock(t, ts < 60), Math.min(Math.max(x, x0 + 16), x1 - 14), y1 + 8);
      }
      if (unit) { c.textAlign = 'left'; c.textBaseline = 'top'; c.fillStyle = D.cv('--ink6'); c.font = font(11, 500); c.fillText(unit, 4, 1); }

      c.save(); c.beginPath(); c.rect(x0 - 2, y0 - 6, x1 - x0 + 4, y1 - y0 + 8); c.clip();
      if (o.stack) {
        const np = lines[0].pts.length, acc = new Array(np).fill(0), surf = D.cv('--card');
        for (let li = 0; li < lines.length; li++) {
          const L = lines[li], lo = acc.slice();
          for (let j = 0; j < np; j++) acc[j] += L.pts[j][1] || 0;
          c.beginPath();
          for (let j = 0; j < np; j++) { const x = X(L.pts[j][0]), y = Y(acc[j]); if (j) c.lineTo(x, y); else c.moveTo(x, y); }
          for (let j = np - 1; j >= 0; j--) c.lineTo(X(L.pts[j][0]), Y(lo[j]));
          c.closePath(); c.fillStyle = D.cv(L.s.color); c.fill();
          if (li < lines.length - 1) { c.beginPath(); for (let j = 0; j < np; j++) { const x = X(L.pts[j][0]), y = Y(acc[j]); if (j) c.lineTo(x, y); else c.moveTo(x, y); } c.strokeStyle = surf; c.lineWidth = 1.5; c.stroke(); }
        }
      } else {
        // threshold lines first, data on top
        for (const [th, v] of ths) {
          const y = Math.round(Y(v)) + 0.5; c.strokeStyle = D.cv('--bad'); c.lineWidth = 1.25; c.setLineDash([5, 4]); c.beginPath(); c.moveTo(x0, y); c.lineTo(x1, y); c.stroke(); c.setLineDash([]);
        }
        c.lineJoin = 'round'; c.lineCap = 'round';
        for (const L of lines) {
          c.strokeStyle = D.cv(L.s.color); c.lineWidth = L.s.dash ? 1.5 : L.bold ? (L.s.heavy ? 2.75 : 2.25) : 1.1; c.globalAlpha = L.bold ? 1 : 0.5;
          c.setLineDash(L.s.dash ? [5, 4] : []);
          c.beginPath(); let pen = false;
          for (const [t, v] of L.pts) { if (v == null) { pen = false; continue; } const x = X(t), y = Y(v); if (pen) c.lineTo(x, y); else { c.moveTo(x, y); pen = true; } }
          c.stroke();
        }
        c.globalAlpha = 1; c.setLineDash([]);
      }
      c.restore();

      // end markers + values of the bold lines
      if (!o.stack && !o.noEnd) {
        const ends = [];
        for (const L of lines) if (L.bold && L.last != null && !L.s.noEnd) ends.push({ L, y: Y(L.last), x: X(L.lastT) });
        for (const e of ends) { c.beginPath(); c.arc(e.x, e.y, 4.5, 0, 6.2832); c.fillStyle = D.cv('--card'); c.fill(); c.beginPath(); c.arc(e.x, e.y, 3.2, 0, 6.2832); c.fillStyle = D.cv(e.L.s.color); c.fill(); }
        ends.sort((a, b) => a.y - b.y);
        for (let i = 0; i < ends.length; i++) { ends[i].ly = ends[i].y; if (i && ends[i].ly < ends[i - 1].ly + 14) ends[i].ly = ends[i - 1].ly + 14; }
        for (let i = ends.length - 1; i >= 0; i--) { const lim = (i === ends.length - 1 ? y1 + 4 : ends[i + 1].ly - 14); if (ends[i].ly > lim) ends[i].ly = lim; }
        c.font = font(12, 700); c.textAlign = 'left'; c.textBaseline = 'middle'; c.fillStyle = D.cv('--ink2');
        for (const e of ends) c.fillText(D.f(e.L.last, dg), x1 + 9, e.ly);
      }
      for (const [th, v] of ths) {
        if (o.stack) continue;
        const y = Y(v), txt = th.label + ' ' + D.f(v, th.digits == null ? 0 : th.digits) + (unit ? ' ' + unit : '');
        c.font = font(11.5, 600); const tw = c.measureText(txt).width;
        c.fillStyle = D.cv('--card'); c.globalAlpha = 0.85; c.fillRect(x0 + 6, y - 17, tw + 8, 15); c.globalAlpha = 1;
        c.fillStyle = D.cv('--ink4'); c.textAlign = 'left'; c.textBaseline = 'middle'; c.fillText(txt, x0 + 10, y - 9);
      }
      // legend values = what the bold line ends at
      for (let i = 0; i < this.lg.length; i++) {
        const s = o.series[i]; let v = null;
        for (const L of lines) if (L.s === s && L.bold) v = L.last;
        this.lg[i].set(v == null ? '–' : D.f(v, dg), unit);
      }
      // crosshair
      if (this.mx != null && this.mx >= x0 - 4 && this.mx <= x1 + 4) {
        const t = tA + (this.mx - x0) / (x1 - x0) * (tB - tA); let a = i0, b = n - 1;
        while (a < b) { const m = (a + b) >> 1; if (ticks[m].t < t) a = m + 1; else b = m; }
        if (a > i0 && Math.abs(ticks[a - 1].t - t) < Math.abs(ticks[a].t - t)) a--;
        const tk = ticks[a], x = Math.round(X(tk.t)) + 0.5;
        c.strokeStyle = D.cv('--ink6'); c.lineWidth = 1; c.beginPath(); c.moveTo(x, y0 - 4); c.lineTo(x, y1); c.stroke();
        const rows = []; let acc = 0;
        for (const s of o.series) {
          const vi = s.inst ? s.inst(tk) : null, vc = (s.cum && tk.rec && !s.plain) ? s.cum(tk) : null;
          let val;
          if (o.stack) { const L = lines.find(l => l.s === s), v = L.fn(tk); val = D.fu(v, dg, unit); acc += v || 0; }
          else if (D.num(vc) && D.num(vi)) val = '누적 ' + D.f(vc, dg) + ' · 순간 ' + D.f(vi, dg) + (unit ? ' ' + unit : '');
          else val = D.fu(D.num(vc) ? vc : vi, dg, unit);
          rows.push([D.cv(s.color), s.label, val]);
          if (!o.stack) for (const L of lines) if (L.s === s && L.bold) { const v = L.fn(tk); if (D.num(v)) { c.beginPath(); c.arc(x, Y(v), 5, 0, 6.2832); c.fillStyle = D.cv('--card'); c.fill(); c.beginPath(); c.arc(x, Y(v), 3.5, 0, 6.2832); c.fillStyle = D.cv(s.color); c.fill(); } }
        }
        if (o.stack) rows.push([null, '합계', D.fu(acc, dg, unit)]);
        this.showTip(x, this.my == null ? y0 : this.my, tk.rec ? '측정 ' + D.mmss(tk.el) + '  ·  ' + D.clock(tk.t) : D.clock(tk.t), rows);
      } else this.tip.style.display = 'none';
    }
  }

  /* Scope: the last 80 slots, x = slot (D/S/U), y = the GPU's 132 SMs. Per slot from the tenant CSV: PDSCH [0,N), free [N,64), ORDER [64,72),
     free [72,min(P,120)), not usable [120,P), PUSCH [P,132); YOLO runs on the first run_cls SMs of the free positions.
     Baseline (MIG): no per-slot data exists, the fixed split is drawn instead. */
  class Scope extends Base {
    constructor(host, opt) { super(host); this.opt = opt || {}; }
    draw(F) {
      this.F = F; if (!this.ready()) return;
      const c = this.begin(), o = this.opt, m = F.meta || {}, sc = F.scope, NSM = m.gpu_sm || 132, ORD = m.order_sm || 8, tdd = m.tdd || 'DDDDDDSUUU';
      const mini = !!o.mini, x0 = 38, x1 = this.w - 8, y0 = 8, y1 = this.h - (mini ? 20 : 40);
      const Y = s => y1 - s / NSM * (y1 - y0);
      const mode = (F.cur && F.cur.sys && F.cur.sys.mode) || m.mode;
      c.font = font(11.5, 500); c.textAlign = 'right'; c.textBaseline = 'middle';
      const ylab = v => { c.fillStyle = D.cv('--ink5'); c.fillText(String(v), x0 - 8, Y(v)); };
      this.tip.style.display = 'none';
      if (!sc || !sc.P) {
        if (mode === 'baseline') return this.mig(c, F, x0, x1, y0, y1);
        this.empty(c, '슬롯 데이터 수신 대기 중 (L1 테넌트 슬롯 CSV)'); return;
      }
      const n = sc.P.length, cw = (x1 - x0) / n, gap = cw > 9 ? 1.5 : 1, bw = Math.max(1, cw - gap);
      const col = { pd: D.cv('--c4'), pu: D.cv('--c3'), od: D.cv('--order'), run: D.cv('--c1'), idle: D.cv('--c1w'), un: D.cv('--unus') };
      const vg = mini ? 0 : 1;
      const seg = (x, a, b, color) => { if (b <= a) return; const ya = Y(a), yb = Y(b); c.fillStyle = color; c.fillRect(x, yb + (b < NSM ? vg / 2 : 0), bw, Math.max(0.5, ya - yb - vg)); };
      let hov = -1; if (this.mx != null && this.mx >= x0 && this.mx < x1 && this.my != null && this.my <= y1 + 4) hov = Math.min(n - 1, Math.floor((this.mx - x0) / cw));
      const parts = i => {
        let P = sc.P[i], N = sc.N[i], run = sc.run[i] || 0;
        if (!(P > 0) || N < 0) { P = NSM; N = 24; }
        const hiEnd = Math.max(72, Math.min(P, 120)), lo = Math.max(0, 64 - N), hi = Math.max(0, hiEnd - 72);
        run = Math.min(run, lo + hi); const rlo = Math.min(run, lo), rhi = run - rlo;
        return { P, N, run, lo, hi, hiEnd, rlo, rhi };
      };
      for (let i = 0; i < n; i++) {
        const x = x0 + i * cw + gap / 2, p = parts(i);
        seg(x, 0, Math.min(p.N, 64), col.pd);
        seg(x, p.N, p.N + p.rlo, col.run); seg(x, p.N + p.rlo, 64, col.idle);
        seg(x, 64, 64 + ORD, col.od);
        seg(x, 72, 72 + p.rhi, col.run); seg(x, 72 + p.rhi, p.hiEnd, col.idle);
        seg(x, p.hiEnd, p.P, col.un);
        seg(x, Math.max(p.P, 72), NSM, col.pu);
        if (i === hov) { c.strokeStyle = D.cv('--ink'); c.lineWidth = 1.5; c.strokeRect(x - 0.75, y0 - 0.75, bw + 1.5, y1 - y0 + 1.5); }
      }
      // axes: SM index left, slot type below, frame boundaries
      ylab(0); ylab(64); if (!mini) ylab(72); ylab(NSM);
      c.textAlign = 'center'; c.textBaseline = 'top';
      for (let i = 0; i < n; i++) {
        const ps = sc.pos[i], xm = x0 + i * cw + cw / 2;
        if (ps === 0) { c.strokeStyle = D.cv('--ink6'); c.lineWidth = 1; c.beginPath(); const xx = Math.round(x0 + i * cw) + 0.5; c.moveTo(xx, y1 + 2); c.lineTo(xx, y1 + (mini ? 6 : 9)); c.stroke(); }
        if (!mini && cw >= 8) { const ch = tdd[ps] || '?'; c.font = font(10.5, ch === 'U' ? 700 : 500); c.fillStyle = D.cv(ch === 'U' ? '--ink3' : ch === 'S' ? '--ink5' : '--ink6'); c.fillText(ch, xm, y1 + 10); }
      }
      c.font = font(11.5, 500); c.fillStyle = D.cv('--ink5'); c.textBaseline = 'bottom';
      c.textAlign = 'left'; c.fillText('← ' + (n * (m.slot_us || 500) / 1000).toFixed(0) + ' ms 전', x0, this.h - 1);
      c.textAlign = 'right'; c.fillText('지금 →', x1, this.h - 1);
      if (!mini) { c.textAlign = 'center'; c.fillText('슬롯 (D = Downlink, S = Special, U = Uplink) · 눈금 = 프레임 경계 (10 슬롯 = 5 ms)', (x0 + x1) / 2, this.h - 1); }
      c.save(); c.translate(10, (y0 + y1) / 2); c.rotate(-Math.PI / 2); c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText('SM', 0, 0); c.restore();
      if (hov >= 0) {
        const p = parts(hov), ch = tdd[sc.pos[hov]] || '?';
        const rows = [[col.pu, 'PUSCH', (NSM - p.P) + ' SM'], [col.od, 'ORDER', ORD + ' SM'], [col.pd, 'PDSCH', p.N + ' SM'],
          [col.run, 'YOLO 실행' + (p.run > 0 && sc.m && sc.m[hov] ? ' (' + sc.m[hov] + ')' : ''), p.run + ' SM'], [col.idle, 'YOLO 제공 · 미사용', (p.lo + p.hi - p.run) + ' SM']];
        if (p.P > p.hiEnd) rows.push([col.un, '사용 불가', (p.P - p.hiEnd) + ' SM']);
        rows.push([null, '대기 요청', String(sc.q ? sc.q[hov] : 0)]);
        this.showTip(x0 + hov * cw + cw / 2, this.my, '슬롯 ' + (sc.s0 + hov) + ' · ' + ch + ' (프레임 내 ' + sc.pos[hov] + ')', rows);
      }
    }
    // Baseline: the static MIG split (4g = L1, 3g = YOLO), the YOLO instance's time share in use as a fill
    mig(c, F, x0, x1, y0, y1) {
      // the y axis is the PHYSICAL GPU (132 SMs): the Baseline YOLO server's welcome reports only its own instance (60)
      const m = F.meta, b = m.baseline || { ran_sm: 64, yolo_sm: 60 }, NSM = Math.max(132, b.ran_sm + b.yolo_sm), g = (F.cur && F.cur.gpu) || {}, busy = D.num(g.busy) ? g.busy / 100 : 0;
      const Y = s => y1 - s / NSM * (y1 - y0);
      const ylab = v => { c.font = font(11.5, 500); c.textAlign = 'right'; c.textBaseline = 'middle'; c.fillStyle = D.cv('--ink5'); c.fillText(String(v), x0 - 8, Y(v)); };
      const r = (a, bb, color, al) => { c.globalAlpha = al == null ? 1 : al; c.fillStyle = color; const ya = Y(a), yb = Y(bb); rr(c, x0, yb + 1.5, x1 - x0, ya - yb - 3, 8); c.fill(); c.globalAlpha = 1; };
      r(0, b.ran_sm, D.cv('--c3w'));
      r(b.ran_sm, b.ran_sm + b.yolo_sm, D.cv('--c1w'));
      if (busy > 0) { c.save(); c.beginPath(); c.rect(x0, y0, (x1 - x0) * busy, y1 - y0); c.clip(); r(b.ran_sm, b.ran_sm + b.yolo_sm, D.cv('--c1')); c.restore(); }
      if (b.ran_sm + b.yolo_sm < NSM) r(b.ran_sm + b.yolo_sm, NSM, D.cv('--unus'));
      ylab(0); ylab(b.ran_sm); ylab(b.ran_sm + b.yolo_sm); if (NSM - b.ran_sm - b.yolo_sm > 10) ylab(NSM);
      c.textAlign = 'left'; c.textBaseline = 'middle'; c.font = font(this.opt.mini ? 13 : 15, 700); c.fillStyle = D.cv('--ink2');
      c.fillText('MIG 4g · L1 (cuBB) 전용 ' + b.ran_sm + ' SM · 고정', x0 + 16, (Y(0) + Y(b.ran_sm)) / 2);
      const yy = (Y(b.ran_sm) + Y(b.ran_sm + b.yolo_sm)) / 2, t1 = 'MIG 3g · YOLO 전용 ' + b.yolo_sm + ' SM · 고정', t2 = '  시간 점유율 ' + D.f(g.busy, 1) + ' %';
      const w1 = c.measureText(t1).width; c.fillStyle = D.cv((x1 - x0) * busy > w1 + 28 ? '--card' : '--ink2'); c.fillText(t1, x0 + 16, yy);
      c.fillStyle = D.cv((x1 - x0) * busy > w1 + c.measureText(t2).width + 28 ? '--card' : '--ink2'); c.fillText(t2, x0 + 16 + w1, yy);
      c.font = font(11.5, 500); c.fillStyle = D.cv('--ink5'); c.textBaseline = 'bottom'; c.textAlign = 'center';
      c.fillText('Baseline은 슬롯 단위 SM 재배분이 없습니다 (MIG 고정 분할). 파란 채움 = YOLO 인스턴스가 추론 중인 시간 비율', (x0 + x1) / 2, this.h - 1);
    }
  }
  function rr(c, x, y, w, h, r) { r = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2); c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }

  D.TimeChart = TimeChart; D.Scope = Scope;
})();
