/* _dgist experiment dashboard - tab "데모 영상" (2026-11-14): the cards that are recorded for the demo video.
   One card = one Capture unit: 4 UE latency meters, the GPU SM split, the PRB slicing bar, 6 time charts.
   Rules kept here:
   - every moving part is drawn on a <canvas> (device-pixel sized, canvas._v counts the drawings), so Capture records exactly what the screen shows;
     card titles and notes are plain DOM text
   - one animation loop for the whole tab; it runs while the tab is on screen or laid out for a capture, a stage draws only when something moves
   - the meters play the per-frame feed (message kind "fr": {k, t, f: {ue: [[frame time s, latency ms | null, QoS met 0/1], ...]}});
     without the feed (older recordings) they follow the last-1-s value of the tick
   - nothing of the other tabs is used except the shared helpers of util.js (D.card, D.cv, D.f, D.frame, D.UE, ...) */
'use strict';
(function () {
  const D = window.D, num = D.num, el = D.el, g = D.g;
  const dpr = () => Math.max(1, Math.min(3, window.devicePixelRatio || 1));
  const font = (px, w) => (w || 500) + ' ' + px + 'px ' + D.FONT;
  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  const TAU = Math.PI * 2, NSM = 132;

  // ---------------------------------------------------------------- tunables
  const T = {
    SLOTS_PER_S: 8,       // SM card: real slots shown per second (slow motion; one real slot is 0.5 ms)
    W_BAR: 22,            // spring of the bars, rad/s (critically damped: settles in about 0.3 s)
    W_NUM: 11,            // spring of the big numbers (slower: digits stay readable)
    VIOL_HOLD: 0.8, VIOL_SHOW: 0.45,       // s the "QoS 초과" state stays after the last violating frame
    RIPPLE_MIN: 0.55,     // s between two pulses of the red outline (below 2 per second)
    RIPPLE_LEN: 0.9,      // s one pulse lasts
    PEAK_S: 3,            // window of the peak-hold marker
    FEED_STALE: 2.5,      // s without the frame feed before a meter follows the 1-s value of the tick
    QUOTA_STEP: 5,        // PRB card: a boundary move of at least this many PRB shows the change chip
    CHIP_S: 1.5,          // s the change chip stays
    SCOPE_STALE: 2.0,     // s without slot data before the SM card goes idle
  };

  // ---------------------------------------------------------------- colour + drawing helpers
  const rgbc = {};
  function rgb(tok) {                      // '--c1' | '#rrggbb' | 'rgb(...)' -> [r, g, b]
    const s = tok.slice(0, 2) === '--' ? D.cv(tok) : tok; let v = rgbc[s];
    if (v) return v;
    if (s[0] === '#') { let h = s.slice(1); if (h.length === 3) h = h.replace(/./g, '$&$&'); const n = parseInt(h.slice(0, 6), 16); v = isFinite(n) ? [(n >> 16) & 255, (n >> 8) & 255, n & 255] : null; }
    else { const m = /(\d+)[ ,]+(\d+)[ ,]+(\d+)/.exec(s); v = m ? [+m[1], +m[2], +m[3]] : null; }
    return (rgbc[s] = v || [128, 128, 128]);
  }
  const rgba = (tok, a) => { const v = rgb(tok); return 'rgba(' + v[0] + ',' + v[1] + ',' + v[2] + ',' + clamp(a, 0, 1).toFixed(3) + ')'; };
  function mix(a, b, k) { const x = rgb(a), y = rgb(b); return 'rgb(' + Math.round(x[0] + (y[0] - x[0]) * k) + ',' + Math.round(x[1] + (y[1] - x[1]) * k) + ',' + Math.round(x[2] + (y[2] - x[2]) * k) + ')'; }
  // rounded rectangle path; r = one radius or [top-left, top-right, bottom-right, bottom-left]
  function rr(c, x, y, w, h, r) {
    if (w < 0) { x += w; w = -w; } if (h < 0) { y += h; h = -h; }
    const m = Math.min(w, h) / 2, q = Array.isArray(r) ? r : [r, r, r, r], a = Math.min(q[0], m), b = Math.min(q[1], m), d = Math.min(q[2], m), e = Math.min(q[3], m);
    c.beginPath(); c.moveTo(x + a, y); c.lineTo(x + w - b, y); c.arcTo(x + w, y, x + w, y + b, b); c.lineTo(x + w, y + h - d); c.arcTo(x + w, y + h, x + w - d, y + h, d);
    c.lineTo(x + e, y + h); c.arcTo(x, y + h, x, y + h - e, e); c.lineTo(x, y + a); c.arcTo(x, y, x + a, y, a); c.closePath();
  }
  function nice(max, nt) {
    if (!(max > 0)) max = 1;
    const raw = max / (nt || 4), p = Math.pow(10, Math.floor(Math.log10(raw))), m = raw / p;
    const st = (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
    return { step: st, max: Math.ceil(max / st - 1e-9) * st };
  }
  // text runs on one baseline: [[text, font, colour], ...]; align 'l' starts at x, 'r' ends at x, 'c' is centred on x. Returns the width.
  function runs(c, parts, x, y, align) {
    let w = 0; for (const p of parts) { c.font = p[1]; p[3] = c.measureText(p[0]).width; w += p[3]; }
    let cx = align === 'r' ? x - w : align === 'c' ? x - w / 2 : x;
    c.textAlign = 'left';
    for (const p of parts) { c.font = p[1]; c.fillStyle = p[2]; c.fillText(p[0], cx, y); cx += p[3]; }
    return w;
  }
  function runsW(c, parts) { let w = 0; for (const p of parts) { c.font = p[1]; w += c.measureText(p[0]).width; } return w; }
  // a pill with text (and an optional leading mark: 'dot' | 'warn' | 'lock'); anchored by its right edge, centre or left edge; returns its width
  function pill(c, text, x, cy, o) {
    const h = o.h || 28, px = o.px || 12, f = font(o.size || 14, 700); c.font = f;
    const tw = c.measureText(text).width, iw = o.mark ? 16 : 0, w = tw + iw + 2 * px, s = o.scale == null ? 1 : o.scale;
    const left = o.align === 'r' ? x - w : o.align === 'c' ? x - w / 2 : x;
    c.save(); c.globalAlpha *= (o.alpha == null ? 1 : o.alpha);
    if (s !== 1) { const ax = o.align === 'r' ? x - w / 2 : left + w / 2; c.translate(ax, cy); c.scale(s, s); c.translate(-ax, -cy); }
    rr(c, left, cy - h / 2, w, h, h / 2); c.fillStyle = o.bg; c.fill();
    if (o.mark) mark(c, o.mark, left + px + 5, cy, o.fg);
    c.font = f; c.fillStyle = o.fg; c.textAlign = 'left'; c.textBaseline = 'middle'; c.fillText(text, left + px + iw, cy + 0.5);
    c.restore();
    return w;
  }
  function mark(c, kind, x, y, col) {
    c.save(); c.fillStyle = col; c.strokeStyle = col; c.lineJoin = 'round'; c.lineCap = 'round';
    if (kind === 'dot') { c.beginPath(); c.arc(x, y, 4, 0, TAU); c.fill(); }
    else if (kind === 'warn') { c.beginPath(); c.moveTo(x, y - 5.5); c.lineTo(x + 6, y + 4.5); c.lineTo(x - 6, y + 4.5); c.closePath(); c.lineWidth = 1.5; c.stroke(); c.fill(); }
    else if (kind === 'ring') { c.lineWidth = 1.75; c.beginPath(); c.arc(x, y, 3.6, 0, TAU); c.stroke(); }
    else if (kind === 'lock') { rr(c, x - 5, y - 1.5, 10, 7.5, 2); c.fill(); c.lineWidth = 1.75; c.beginPath(); c.arc(x, y - 2, 3.1, Math.PI, TAU); c.stroke(); }
    c.restore();
  }
  // critically damped spring, integrated exactly (any dt is stable)
  class Sp {
    constructor(x) { this.x = x == null ? null : x; this.v = 0; this.to = this.x; }
    go(target, dt, w) {
      this.to = target;
      if (this.x == null || !isFinite(this.x)) { this.x = target; this.v = 0; return this.x; }
      const d = this.x - target, e = Math.exp(-w * dt), k = (this.v + w * d) * dt;
      this.x = target + (d + k) * e; this.v = (this.v - w * k) * e;
      return this.x;
    }
    snap(x) { this.x = x; this.v = 0; this.to = x; }
    moving(eps) { return this.x != null && (Math.abs(this.x - this.to) > (eps || 0.05) || Math.abs(this.v) > (eps || 0.05) * 4); }
  }
  const backOut = k => { k = clamp(k, 0, 1); const s = 1.9, q = k - 1; return 1 + (s + 1) * q * q * q + s * q * q; };   // pops a little past 1, then settles

  // ---------------------------------------------------------------- stages + the one animation loop
  const stages = []; let raf = 0, tPrev = 0, tabEl = null, wd = 0, failed = false;
  function tabActive() { const e = tabEl || (tabEl = document.getElementById('tab-demo')); return !!e && (e.classList.contains('on') || e.classList.contains('capon')); }
  function runFrame(now) {
    const dt = clamp((now - tPrev) / 1000, 0, 0.5); tPrev = now;
    if (!tabActive() || failed) return;
    try { Feed.pump(now); for (const s of stages) s.frame(now, dt); }
    catch (e) { failed = true; console.error('demo tab:', e); }       // never break the page: stop this tab's drawing and say so once
  }
  function loop(now) { raf = 0; if (!stages.length) return; runFrame(now); raf = requestAnimationFrame(loop); }
  function kick() {
    if (!raf && stages.length) { tPrev = performance.now(); raf = requestAnimationFrame(loop); }
    if (!wd) wd = setInterval(() => { if (!stages.length) { clearInterval(wd); wd = 0; } else if (document.hidden) runFrame(performance.now()); }, 250);   // a hidden page gets no animation frames
  }
  class Stage {
    constructor(host, first) {
      this.host = host; this.cvs = document.createElement('canvas');
      if (first) host.insertBefore(this.cvs, host.firstChild); else host.appendChild(this.cvs);
      this.ctx = this.cvs.getContext('2d'); this.w = 0; this.h = 0; this.d = 1; this.dirty = true; this.resized = true; this.F = null;
      this.ro = new ResizeObserver(() => { this.resized = true; }); this.ro.observe(host);
      D.charts.push(this); stages.push(this); kick();
    }
    size() {
      const w = Math.round(this.host.clientWidth), h = Math.round(this.host.clientHeight), d = dpr();
      if (!w || !h) return false;
      if (w === this.w && h === this.h && d === this.d) return false;
      this.w = w; this.h = h; this.d = d; this.cvs.width = Math.round(w * d); this.cvs.height = Math.round(h * d); return true;
    }
    destroy() { this.ro.disconnect(); const i = stages.indexOf(this); if (i >= 0) stages.splice(i, 1); this.F = null; this.dead = true; }
    redraw() { this.dirty = true; }
    data(F) { this.F = F; this.dirty = true; }
    begin() { const c = this.ctx; this.cvs._v = (this.cvs._v | 0) + 1; c.setTransform(this.d, 0, 0, this.d, 0, 0); c.clearRect(0, 0, this.w, this.h); c.globalAlpha = 1; c.setLineDash([]); c.shadowBlur = 0; c.shadowColor = 'transparent'; return c; }
    frame(now, dt) {
      if (this.resized || dpr() !== this.d) { this.resized = false; if (this.size()) { this.dirty = true; if (this.layout) this.layout(); } }
      if (!this.w) return;
      const moving = this.step(now, dt);
      if (moving || this.dirty) { this.dirty = false; this.draw(now); }
    }
    step() { return false; }
  }

  // ---------------------------------------------------------------- the per-frame feed: live messages or the "fr" lines of a recording
  const Feed = (function () {
    const P = {}; let R = null, wasRp = false;
    for (const u of D.UE) P[u.k] = { q: [], tail: 0, lastB: 0, iv: 0.2, liveAt: 0, subs: [] };
    function clearQ() { for (const k in P) { P[k].q.length = 0; P[k].tail = 0; P[k].lastB = 0; } }
    // frames of one batch are played evenly over the next batch interval
    function ingest(k, arr, now) {
      const p = P[k]; if (!p || !Array.isArray(arr) || !arr.length) return;
      const gap = p.lastB ? (now - p.lastB) / 1000 : p.iv; p.lastB = now;
      p.iv = clamp(0.6 * p.iv + 0.4 * clamp(gap, 0.12, 1.5), 0.12, 1.5);
      if (p.tail - now > 1000) { p.q.length = 0; p.tail = 0; }                       // a backlog: show the newest frames only
      const start = Math.max(now, Math.min(p.tail, now + 250));
      if (p.q.length) { const n = p.q.length; for (let i = 0; i < n; i++) p.q[i].at = now + (start - now) * (i + 1) / n; }   // what is still waiting finishes before the new batch starts
      const step = p.iv * 1000 / arr.length;
      for (let i = 0; i < arr.length; i++) { const f = arr[i]; if (Array.isArray(f)) p.q.push({ at: start + step * i, t: f[0], v: num(f[1]) ? f[1] : null, met: !!f[2] }); }
      p.tail = start + step * arr.length;
    }
    function pumpReplay(rp, now) {
      const frs = rp.frs;
      if (!frs || !frs.length) { if (!R || R.rp !== rp) { R = { rp, none: true }; clearQ(); } return; }
      const t = rp.t0 + rp.pos + 1e-6;
      if (!R || R.rp !== rp || R.none || t < R.t - 1e-3 || t > R.t + 0.2 * (rp.speed || 1) + 2) {      // a new replay or a seek: go on from here
        let a = 0, b = frs.length; while (a < b) { const m = (a + b) >> 1; if (frs[m].t <= t) a = m + 1; else b = m; }
        R = { rp, idx: a, t }; clearQ(); return;
      }
      if (t <= R.t) return;
      R.t = t; let acc = null;
      while (R.idx < frs.length && frs[R.idx].t <= t) {
        const f = frs[R.idx++].f; if (!f) continue;
        for (const k in f) if (Array.isArray(f[k])) { if (!acc) acc = {}; acc[k] = (acc[k] || []).concat(f[k]); }
      }
      if (acc) for (const k in acc) ingest(k, acc[k], now);
    }
    return {
      // a live "fr" message (app.js onMsg)
      live(m) {
        if (!m || !m.f) return; const now = performance.now(), play = !D.S.rp && stages.length && tabActive() && !document.hidden;
        for (const k in m.f) { const p = P[k]; if (!p) continue; p.liveAt = now; if (play) ingest(k, m.f[k], now); else { p.q.length = 0; p.tail = 0; } }
      },
      pump(now) {
        const rp = D.S.rp;
        if (!!rp !== wasRp) { wasRp = !!rp; clearQ(); R = null; }
        if (rp) pumpReplay(rp, now);
        for (const k in P) {
          const p = P[k]; let n = 0;
          while (p.q.length && p.q[0].at <= now && n++ < 240) { const f = p.q.shift(); for (const s of p.subs) s.onFrame(f, now); }
        }
      },
      // is a frame feed available for this UE right now?
      has(k, now) { const rp = D.S.rp; return rp ? !!(rp.frs && rp.frs.length) : (P[k].liveAt > 0 && now - P[k].liveAt < T.FEED_STALE * 1000); },
      sub(k, s) { P[k].subs.push(s); }, unsub(k, s) { const i = P[k].subs.indexOf(s); if (i >= 0) P[k].subs.splice(i, 1); },
    };
  })();

  // digits in equal cells, so a changing number does not shake what follows it; returns the width
  let cwc = {};
  function digW(c, f) { let w = cwc[f]; if (!w) { c.font = f; w = 0; for (let i = 0; i < 10; i++) w = Math.max(w, c.measureText(String(i)).width); cwc[f] = w; } return w; }
  function tnumW(c, s, f) { const cw = digW(c, f); c.font = f; let w = 0; for (const ch of s) w += (ch >= '0' && ch <= '9') ? cw : c.measureText(ch).width; return w; }
  function tnum(c, s, x, y, f, col, align) {
    const cw = digW(c, f), w = tnumW(c, s, f); c.font = f; c.fillStyle = col; c.textAlign = 'center';
    let cx = align === 'r' ? x - w : align === 'c' ? x - w / 2 : x;
    for (const ch of s) { const q = (ch >= '0' && ch <= '9') ? cw : c.measureText(ch).width; c.fillText(ch, cx + q / 2, y); cx += q; }
    c.textAlign = 'left'; return w;
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { cwc = {}; for (const s of stages) s.dirty = true; });

  // ---------------------------------------------------------------- UE latency meter (one canvas under the whole card)
  // The red glow inside the card edge, rendered once per card size and colour (in CSS pixels; it is blurred, so scaling it up loses nothing):
  // a blurred shadow drawn anew for every picture costs 1-3 ms per meter where the browser has no GPU, which takes frames from the page and from Capture.
  const glows = new Map();
  function glowSprite(w, h, R, blur, col) {
    const key = w + '|' + h + '|' + R + '|' + blur + '|' + col; let s = glows.get(key);
    if (!s) {
      if (glows.size >= 24) glows.clear();
      s = document.createElement('canvas'); s.width = Math.max(1, w); s.height = Math.max(1, h);
      const c = s.getContext('2d'); c.strokeStyle = col; c.lineWidth = 6; c.shadowColor = col; c.shadowBlur = blur;
      rr(c, 0, 0, w, h, R); c.clip(); rr(c, -3, -3, w + 6, h + 6, R + 3); c.stroke();
      glows.set(key, s);
    }
    return s;
  }
  class Meter extends Stage {
    constructor(card, body, u, meta) {
      super(card, true); this.cvs.className = 'dm-full'; this.body = body; this.u = u; this.va = u.svc === 'va';
      const dl = this.va ? meta.deadline_ms : meta.nv_deadline_ms; this.dl = num(dl) && dl > 0 ? dl : (this.va ? 200 : 250); this.max = 2 * this.dl;
      this.val = new Sp(0); this.numv = new Sp(null); this.peak = new Sp(null); this.violA = new Sp(0); this.dim = new Sp(1); this.over = new Sp(0);
      this.target = null; this.hist = []; this.pk = null; this.inViol = false; this.why = 'late'; this.state = 'idle'; this.fb = false; this.on = false;
      this.lastViol = -1e9; this.rippleAt = -1e9; this.ripples = []; this.nudgeAt = -1e9; this.pillAt = -1e9; this.fed = -1e9; this.tickT = null;
      this.bx = 22; this.by = 56; this.bw = 0; this.bh = 148; this.latch = -1e9; this.pend = null; this.fm = []; this.fq = null;
      Feed.sub(u.k, this);
    }
    destroy() { super.destroy(); Feed.unsub(this.u.k, this); }
    layout() { const a = this.cvs.getBoundingClientRect(), b = this.body.getBoundingClientRect(); this.bx = b.left - a.left; this.by = b.top - a.top; this.bw = b.width; this.bh = b.height; }
    onFrame(f, now) {                                  // one finalised frame of this UE
      this.fed = now; this.fm.push([now, f.met ? 1 : 0]);
      if (f.v != null) {
        this.hist.push([now, f.v]);
        // a frame over the deadline stays on show for VIOL_SHOW (one frame lasts ~66 ms: too short to see); frames in time wait behind it
        if (!f.met) { this.target = f.v; this.latch = now + T.VIOL_SHOW * 1000; this.pend = null; }
        else if (now < this.latch) this.pend = f.v;
        else this.target = f.v;
      }
      if (!f.met) this.violate(now, f.v == null ? 'fail' : 'late');
    }
    violate(now, why) {
      this.why = why; this.lastViol = now;
      if (!this.inViol) { this.inViol = true; this.nudgeAt = now; }                                  // one nudge, on the way into the violation only
      if (now - this.rippleAt >= T.RIPPLE_MIN * 1000) { this.rippleAt = now; this.ripples.push(now); }
    }
    step(now, dt) {
      const F = this.F, t = F && F.cur, ue = t && t.ue ? t.ue[this.u.k] : null;
      const on = (!!ue && (ue.on != null ? !!ue.on : ue.n > 0)) || now - this.fed < 1000, feed = Feed.has(this.u.k, now);
      this.fb = false; this.ue = ue;
      if (on && !feed && ue) {                         // no frame feed (an older recording): follow the last-1-s value of the tick
        const v = ue[this.va ? 'e2e' : 'ul']; this.fb = true;
        if (num(v)) { this.target = v; if (this.tickT !== t.t) { this.tickT = t.t; this.hist.push([now, v]); } if (v > this.dl) this.violate(now, 'late'); }
      }
      if (this.pend != null && now >= this.latch) { this.target = this.pend; this.pend = null; }
      if (!on) { this.target = null; this.hist.length = 0; this.pend = null; this.latch = -1e9; }
      // the QoS met rate of the last second comes from the same frames the bar shows, so it never contradicts the bar; without the feed, the tick's value
      while (this.fm.length && now - this.fm[0][0] > 1000) this.fm.shift();
      { let m = 0; for (const q of this.fm) m += q[1]; this.fq = this.fm.length ? 100 * m / this.fm.length : null; }
      if (this.inViol && now - this.lastViol > T.VIOL_HOLD * 1000) this.inViol = false;
      while (this.hist.length && now - this.hist[0][0] > T.PEAK_S * 1000) this.hist.shift();
      let pk = null; for (const q of this.hist) if (pk == null || q[1] > pk) pk = q[1];
      this.pk = pk; this.on = on;
      const has = this.target != null, st = !on ? 'idle' : this.inViol ? (this.why === 'fail' ? 'fail' : 'bad') : has ? 'ok' : 'wait';
      if (st !== this.state) { this.state = st; this.pillAt = now; }
      this.val.go(has ? Math.min(this.target, this.max * 1.01) : 0, dt, T.W_BAR);
      if (has) this.numv.go(this.target, dt, now < this.latch ? 24 : T.W_NUM); else this.numv.snap(null);
      if (pk != null) this.peak.go(Math.min(pk, this.max), dt, 14); else this.peak.snap(null);
      this.violA.go(this.inViol ? 1 : 0, dt, 20); this.dim.go(on ? 0 : 1, dt, 10); this.over.go(has && this.target > this.max ? 1 : 0, dt, 16);
      while (this.ripples.length && now - this.ripples[0] > T.RIPPLE_LEN * 1000) this.ripples.shift();
      return this.val.moving(0.2) || this.numv.moving(0.3) || this.peak.moving(0.2) || this.violA.moving(0.01) || this.dim.moving(0.01) || this.over.moving(0.01) ||
        this.ripples.length > 0 || now - this.nudgeAt < 500 || now - this.pillAt < 400;
    }
    draw(now) {
      const c = this.begin(), w = this.w, h = this.h, R = parseFloat(D.cv('--r')) || 20, va = clamp(this.violA.x, 0, 1);
      const ink = D.cv('--ink'), ink2 = D.cv('--ink2'), ink4 = D.cv('--ink4'), ink5 = D.cv('--ink5');
      // QoS exceeded: a light wash, a glow that pulses inwards from the card edge (the stroke itself lies outside the clip), a crisp outline
      if (va > 0.005) {
        rr(c, 0, 0, w, h, R); c.fillStyle = rgba('--bad-weak', 0.62 * va); c.fill();
        c.save(); rr(c, 0, 0, w, h, R); c.clip();
        const col = D.cv('--bad'), glow = (blur, a) => { if (a <= 0.004) return; c.globalAlpha = Math.min(1, a); c.drawImage(glowSprite(w, h, R, blur, col), 0, 0, w, h); };
        glow(12, 0.55 * va);
        for (const t0 of this.ripples) {                 // a ripple widens from 14 to 54 px of blur while it fades: the two nearest ready-made glows, mixed
          const k = (now - t0) / (T.RIPPLE_LEN * 1000); if (!(k >= 0 && k < 1)) continue;
          const a = 0.8 * (1 - k) * (1 - k), q = clamp(4 * (1 - Math.pow(1 - k, 3)), 0, 4), i = Math.min(3, Math.floor(q)), f = q - i;
          glow(14 + 10 * i, a * (1 - f)); glow(24 + 10 * i, a * f);
        }
        c.restore();
        c.lineWidth = 2.5; c.strokeStyle = rgba('--bad', va); rr(c, 1.25, 1.25, w - 2.5, h - 2.5, R - 1.25); c.stroke();
      }
      const x = this.bx, y = this.by, bw = this.bw, has = this.target != null && this.numv.x != null, ue = this.ue, st = this.state;
      // status pill on the header line
      {
        const k = (now - this.pillAt) / 320, bad = st === 'bad' || st === 'fail';
        const o = bad ? { bg: D.cv('--bad'), fg: '#ffffff', mark: 'warn', scale: 0.72 + 0.28 * backOut(k) }
          : st === 'ok' ? { bg: D.cv('--good-weak'), fg: D.cv('--good-ink'), mark: 'dot' }
          : { bg: D.cv('--hair'), fg: ink4, mark: 'ring' };
        o.align = 'r'; o.h = 30; o.size = 14.5; o.px = 13; o.alpha = bad ? 1 : clamp(0.35 + k, 0, 1);
        pill(c, st === 'bad' ? 'QoS 초과' : st === 'fail' ? '프레임 실패' : st === 'ok' ? 'QoS 충족' : st === 'wait' ? '수신 중' : '대기 중', x + bw, 32, o);
      }
      let nx = 0; { const q = (now - this.nudgeAt) / 1000; if (q >= 0 && q < 0.5) nx = 6 * Math.exp(-q / 0.11) * Math.sin(TAU * q / 0.13); }
      c.save(); c.globalAlpha = 1 - 0.5 * clamp(this.dim.x, 0, 1); c.translate(nx, 0);
      // the momentary latency
      c.textBaseline = 'alphabetic';
      const fN = font(46, 800), sN = has ? String(Math.round(this.numv.x)) : '–';
      let nw = tnum(c, sN, x, y + 42, fN, has && this.numv.x > this.dl + 0.5 ? D.cv('--bad-ink') : ink, 'l');
      if (has) { c.font = font(19, 600); c.fillStyle = ink4; c.fillText('ms', x + nw + 7, y + 42); nw += 7 + c.measureText('ms').width; }
      // one line on the right: QoS met rate of the last second, the model in use
      {
        const f1 = font(14.5, 500), f2 = font(14.5, 700), q = !this.fb && this.fq != null ? D.f(this.fq, 0) + ' %' : ue && num(ue.qos) ? D.f(ue.qos, 0) + ' %' : '–';
        let parts = !this.on ? [['프레임 없음', f1, ink5]] : [['최근 1초 QoS 충족 ', f1, ink4], [q, f2, ink2]];
        if (this.on && this.fb) parts = [['1초 평균값', f1, ink5], ['  ·  ', f1, D.cv('--ink6')]].concat(parts);
        const more = this.on && this.va && ue && ue.model ? [['  ·  ', f1, D.cv('--ink6')], ['모델 ', f1, ink4], [String(ue.model), f2, ink2]] : null;
        const room = bw - nw - 28;
        if (more && runsW(c, parts.concat(more)) <= room) parts = parts.concat(more);
        if (runsW(c, parts) <= room) runs(c, parts, x + bw, y + 40, 'r');
      }
      // the bar: 0 .. 2 x deadline, the QoS line in the middle
      const by = y + 78, bh = 30, X = v => x + clamp(v / this.max, 0, 1) * bw, xq = X(this.dl), fw = clamp(this.val.x / this.max, 0, 1) * bw;
      rr(c, x, by, bw, bh, bh / 2); c.fillStyle = va > 0.5 ? mix('--hair', '--card', 0.5) : D.cv('--hair'); c.fill();
      if (fw > 0.5) {
        c.save(); rr(c, x, by, bw, bh, bh / 2); c.clip(); rr(c, x - bh, by, fw + bh, bh, bh / 2); c.clip();
        c.fillStyle = D.cv(this.u.color); c.fillRect(x, by, Math.min(fw, xq - x), bh);
        if (x + fw > xq) { c.fillStyle = D.cv('--bad'); c.fillRect(xq, by, x + fw - xq + 1, bh); }
        const oa = clamp(this.over.x, 0, 1);
        if (oa > 0.02) { c.strokeStyle = 'rgba(255,255,255,' + (0.9 * oa).toFixed(3) + ')'; c.lineWidth = 2.25; c.lineCap = 'round'; c.lineJoin = 'round';
          for (let i = 0; i < 3; i++) { const cx = x + bw - 16 - i * 10; c.beginPath(); c.moveTo(cx - 3.5, by + 9); c.lineTo(cx + 2.5, by + bh / 2); c.lineTo(cx - 3.5, by + bh - 9); c.stroke(); } }
        c.restore();
      }
      // QoS line + its label
      c.fillStyle = va > 0.5 ? mix('--bad-weak', '--card', 0.38) : D.cv('--card'); c.fillRect(xq - 3, by - 1, 6, bh + 2);
      c.fillStyle = ink2; rr(c, xq - 1.25, by - 8, 2.5, bh + 16, 1.25); c.fill();
      c.textBaseline = 'middle';
      runs(c, [['QoS ', font(13.5, 600), ink4], [D.f(this.dl, 0) + ' ms', font(13.5, 700), ink2]], xq, by - 19, 'c');
      // scale ends, peak of the last seconds
      const ly = by + bh + 22, f13 = font(13, 500);
      let pl = null;
      if (this.peak.x != null && this.pk != null) {
        const xp = X(this.peak.x), parts = [['최근 ' + T.PEAK_S + '초 최대 ', f13, ink5], [D.f(this.pk, 0) + ' ms', font(13, 700), D.cv('--ink3')]], tw = runsW(c, parts);
        c.fillStyle = D.cv('--ink3'); rr(c, xp - 1, by - 3, 2, bh + 6, 1); c.fill();
        c.beginPath(); c.moveTo(xp, by + bh + 5); c.lineTo(xp + 4.5, by + bh + 11); c.lineTo(xp - 4.5, by + bh + 11); c.closePath(); c.fill();
        const cx = clamp(xp, x + tw / 2, x + bw - tw / 2); runs(c, parts, cx, ly, 'c'); pl = [cx - tw / 2 - 10, cx + tw / 2 + 10];
      }
      c.font = f13; c.fillStyle = ink5; c.textBaseline = 'middle';
      if (!pl || pl[0] > x + 14) { c.textAlign = 'left'; c.fillText('0', x + 2, ly); }
      const mt = D.f(this.max, 0) + ' ms', mw = c.measureText(mt).width;
      if (!pl || pl[1] < x + bw - mw) { c.textAlign = 'right'; c.fillText(mt, x + bw, ly); }
      c.textAlign = 'left';
      c.restore();
    }
  }

  // ---------------------------------------------------------------- GPU SM split: 132 SMs = 무선 신호 처리 | 미배정 | AI
  /* The same definitions as the slot map of the GPU tab (charts.js Scope, hub.py on_slots). Per slot the scheduler gives P and N:
     PDSCH [0,N) + ORDER [64,72) + PUSCH [P,132) = reserved for the RAN ("무선 신호 처리"), [N,64) + [72,min(P,120)) = offered to YOLO ("AI"),
     [120,P) = usable by neither ("미배정"). Baseline = the fixed MIG split of meta.baseline (64 | 60, the remaining 8 unassigned). */
  function slotSplit(P, N) {
    if (!(P > 0) || N < 0 || !num(N)) { P = NSM; N = 24; }
    const hiEnd = Math.max(72, Math.min(P, 120)), ai = Math.max(0, 64 - N) + Math.max(0, hiEnd - 72), un = Math.max(0, P - hiEnd);
    return { ai, un, ran: NSM - ai - un };
  }
  const KIND = { D: 'DL 슬롯', U: 'UL 슬롯', S: 'S 슬롯' };
  class SmBar extends Stage {
    constructor(host, meta) {
      super(host); this.meta = meta; this.tdd = String(meta.tdd || 'DDDDDDSUUU'); this.L = this.tdd.length || 10;
      this.ran = new Sp(null); this.ai = new Sp(null); this.hl = new Sp(null); this.dim = new Sp(1); this.unA = new Sp(0);
      this.sc = null; this.next = null; this.seen = null; this.idx = 0; this.acc = 0; this.cur = null; this.avg = null; this.mode = 'idle';
      this.scAt = -1e9; this.wrapAt = -1e9; this.born = performance.now();
    }
    scope(F) {
      const sc = F && F.scope;
      if (sc && sc !== this.seen && Array.isArray(sc.P) && sc.P.length && Array.isArray(sc.N)) { this.seen = sc; this.next = sc; this.scAt = performance.now(); }
    }
    data(F) { super.data(F); this.scope(F); }
    advance() {                                         // one real slot further
      if (this.next) {                                  // a new message: go on at the next position of the TDD frame, so the rhythm stays
        const sc = this.next; this.next = null; let j = 0;
        if (this.cur && Array.isArray(sc.pos)) { j = sc.pos.indexOf((this.cur.pos + 1) % this.L); if (j < 0) j = 0; }
        this.sc = sc; this.idx = j;
        let s = 0; for (let i = 0; i < sc.P.length; i++) s += slotSplit(sc.P[i], sc.N[i]).ai; this.avg = s / sc.P.length;
      } else if (this.sc && this.idx < this.sc.P.length - 1) this.idx++;
      else return;
      const sp = slotSplit(this.sc.P[this.idx], this.sc.N[this.idx]), ps = Array.isArray(this.sc.pos) ? this.sc.pos[this.idx] : null;
      sp.pos = num(ps) ? ((ps % this.L) + this.L) % this.L : (this.cur ? (this.cur.pos + 1) % this.L : 0);
      this.cur = sp; this.dirty = true;
    }
    step(now, dt) {
      const F = this.F, t = F && F.cur, m = (F && F.meta) || this.meta, mode = (t && t.sys && t.sys.mode) || m.mode, bl = m.baseline || {};
      const rp = D.S.rp, paused = !!(F && F.replay && rp && !rp.playing);
      let tr = null, ta = null, st = 'idle';
      if (mode === 'baseline') { st = 'mig'; tr = num(bl.ran_sm) ? bl.ran_sm : 64; ta = num(bl.yolo_sm) ? bl.yolo_sm : 60; }
      else {
        if (!paused) { this.acc += dt * T.SLOTS_PER_S; let n = 0; while (this.acc >= 1 && n++ < 8) { this.acc -= 1; this.advance(); } if (this.acc >= 1) this.acc = 0; }
        const sm = t && t.gpu ? t.gpu.sm : null;
        if (this.cur && (paused || now - this.scAt < T.SCOPE_STALE * 1000)) { st = 'slots'; tr = this.cur.ran; ta = this.cur.ai; }
        else if (!this.seen && sm && num(sm.free) && (F.replay || now - this.born > 2000)) {          // a recording without slot lines: the 1-s means of the tick
          st = 'tick'; ta = sm.free; tr = num(sm.ran) ? sm.ran : NSM - ta - (num(sm.unus) ? sm.unus : 0);
        }
      }
      if (st !== this.mode) { this.mode = st; this.dirty = true; }
      if (st !== 'idle') { const wb = st === 'slots' ? 26 : T.W_BAR; this.ran.go(tr, dt, wb); this.ai.go(ta, dt, wb); }
      this.dim.go(st === 'idle' ? 1 : 0, dt, 10);
      const un = this.ran.x != null ? NSM - this.ran.to - this.ai.to : 0; this.unA.go(un >= 0.5 ? 1 : 0, dt, 18);
      if (st === 'slots') { const p = this.cur.pos; if (this.hl.x == null || p < this.hl.to - 0.5) { this.hl.snap(p); this.wrapAt = now; } else this.hl.go(p, dt, 46); }
      return this.ran.moving(0.02) || this.ai.moving(0.02) || this.dim.moving(0.01) || this.unA.moving(0.01) || (st === 'slots' && (this.hl.moving(0.01) || now - this.wrapAt < 160));
    }
    draw(now) {
      const c = this.begin(), w = this.w, st = this.mode, narrow = w < 700, m = (this.F && this.F.meta) || this.meta;
      const ink = D.cv('--ink'), ink2 = D.cv('--ink2'), ink4 = D.cv('--ink4'), ink5 = D.cv('--ink5'), cR = D.cv('--c3'), cA = D.cv('--c1'), card = D.cv('--card');
      const has = this.ran.x != null && this.ai.x != null, ran = has ? clamp(this.ran.x, 0, NSM) : 0, ai = has ? clamp(this.ai.x, 0, NSM - ran) : 0, dec = st === 'tick' ? 1 : 0;
      c.globalAlpha = 1 - 0.5 * clamp(this.dim.x, 0, 1);
      // the two sides: name, SM count, share
      const fL = font(15, 700), fN = font(40, 800), fU = font(17, 600), fS = font(14.5, 600);
      c.textBaseline = 'middle'; c.fillStyle = cR; rr(c, 0, 4, 12, 12, 4); c.fill(); c.font = fL; c.fillStyle = ink2; c.textAlign = 'left'; c.fillText('무선 신호 처리', 19, 10.5);
      c.fillStyle = cA; rr(c, w - 12, 4, 12, 12, 4); c.fill(); c.font = fL; c.fillStyle = ink2; c.textAlign = 'right'; c.fillText('AI', w - 19, 10.5); c.textAlign = 'left';
      c.textBaseline = 'alphabetic';
      const sR = has ? D.f(ran, dec) : '–', sA = has ? D.f(ai, dec) : '–';
      let nw = tnum(c, sR, 0, 62, fN, ink, 'l'); c.font = fU; c.fillStyle = ink4; c.fillText('SM', nw + 6, 62); nw += 6 + c.measureText('SM').width;
      if (has) tnum(c, D.f(100 * ran / NSM, 0) + ' %', nw + 12, 62, fS, ink5, 'l');
      c.font = fU; const uw = c.measureText('SM').width; c.fillStyle = ink4; c.fillText('SM', w - uw, 62);
      const aw = tnum(c, sA, w - uw - 6, 62, fN, ink, 'r');
      if (has) tnum(c, D.f(100 * ai / NSM, 0) + ' %', w - uw - 6 - aw - 12, 62, fS, ink5, 'r');
      // the chip in the middle: which slot is shown / the fixed split
      {
        const kd = st === 'slots' ? this.tdd[this.cur.pos] : null, f = font(14, 700); c.font = f;
        const txt = st === 'slots' ? (KIND[kd] || '슬롯') + ' · ' + (this.cur.pos + 1) + '/' + this.L : st === 'mig' ? 'MIG 고정 분할' : st === 'tick' ? '최근 1초 평균' : '대기 중';
        const wide = st === 'slots' ? 'DL 슬롯 · ' + this.L + '/' + this.L : txt, tw = Math.max(c.measureText(txt).width, c.measureText(wide).width), mk = st === 'mig' ? 18 : 0, pw = tw + mk + 28, cy = 14;
        rr(c, w / 2 - pw / 2, cy - 14, pw, 28, 14); c.fillStyle = st === 'slots' && kd === 'U' ? D.cv('--c1w') : D.cv('--hair'); c.fill();
        if (mk) mark(c, 'lock', w / 2 - pw / 2 + 14 + 5, cy + 1, D.cv('--ink3'));
        c.font = f; c.fillStyle = D.cv('--ink3'); c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(txt, w / 2 + mk / 2, cy + 0.5);
        const cap = st === 'slots' ? '실제 슬롯 ' + D.f((m.slot_us || 500) / 1000, 1) + ' ms · 느린 재생' : st === 'mig' ? 'Baseline · 슬롯별 재배분 없음' : st === 'tick' ? '슬롯별 기록이 없는 녹화' : '슬롯 데이터 수신 대기 중';
        c.font = font(13, 500); c.fillStyle = ink5; c.fillText(cap, w / 2, 45); c.textAlign = 'left';
      }
      // the bar
      const by = 84, bh = 46, R = 14, r2 = 5, gp = 4, xr = ran / NSM * w, xa = w - ai / NSM * w, mid = xa - xr > gp + 3;
      if (!has) { rr(c, 0, by, w, bh, R); c.fillStyle = D.cv('--hair'); c.fill(); }
      else {
        const bR = mid ? xr : (xr + xa) / 2, bA = mid ? xa : bR;
        if (bR - gp / 2 > 1) { rr(c, 0, by, bR - gp / 2, bh, [R, r2, r2, R]); c.fillStyle = cR; c.fill(); }
        if (w - bA - gp / 2 > 1) { rr(c, bA + gp / 2, by, w - bA - gp / 2, bh, [r2, R, R, r2]); c.fillStyle = cA; c.fill(); }
        const ua = clamp(this.unA.x, 0, 1), un = NSM - this.ran.to - this.ai.to, us = D.f(Math.max(0, un), dec);
        if (mid) {
          rr(c, xr + gp / 2, by, xa - xr - gp, bh, r2); c.fillStyle = D.cv('--unus'); c.fill();
          c.save(); c.globalAlpha *= ua; c.textBaseline = 'middle';
          const parts = [['미배정 ', font(13, 600), ink4], [us + ' SM', font(13, 700), D.cv('--ink3')]], tw = runsW(c, parts), cx = (xr + xa) / 2;
          if (st !== 'mig' && xa - xr - gp >= tw + 14) runs(c, parts, cx, by + bh / 2 + 0.5, 'c');
          else { c.fillStyle = D.cv('--ink6'); c.fillRect(cx - 0.75, by + bh + 4, 1.5, 6); runs(c, parts, clamp(cx, tw / 2, w - tw / 2), by + bh + 21, 'c'); }
          c.restore();
        }
        if (st === 'mig') {                             // a lock on the fixed boundary
          const cx = mid ? (xr + xa) / 2 : bA, cy = by + bh / 2;
          c.beginPath(); c.arc(cx, cy, 16, 0, TAU); c.fillStyle = card; c.fill(); c.lineWidth = 1.5; c.strokeStyle = D.cv('--ink6'); c.stroke(); mark(c, 'lock', cx, cy + 0.5, D.cv('--ink3'));
        } else {                                        // the handle on the AI boundary
          rr(c, bA - 4.5, by - 7, 9, bh + 14, 4.5); c.fillStyle = card; c.fill(); c.lineWidth = 1.5; c.strokeStyle = D.cv('--ink4'); c.stroke();
        }
      }
      c.font = font(13, 500); c.fillStyle = ink5; c.textBaseline = 'middle'; c.textAlign = 'left'; c.fillText('0', 1, by + bh + 21); c.textAlign = 'right'; c.fillText(NSM + ' SM', w, by + bh + 21); c.textAlign = 'left';
      // the TDD frame with the slot on show, the mean of the window
      const ch = 30, cw = narrow ? 24 : 34, cg = 4, tw = this.L * cw + (this.L - 1) * cg, tx = narrow ? 0 : Math.round((w - tw) / 2), ty = by + bh + 42, hi = st === 'slots' && this.hl.x != null ? this.hl.x : null;
      if (!narrow) { c.font = font(13, 600); c.fillStyle = ink5; c.textAlign = 'right'; c.fillText('TDD 패턴', tx - 12, ty + ch / 2 + 0.5); c.textAlign = 'left'; }
      for (let i = 0; i < this.L; i++) { rr(c, tx + i * (cw + cg), ty, cw, ch, 8); c.fillStyle = D.cv('--hair'); c.fill(); }
      if (hi != null) { c.save(); c.globalAlpha *= clamp(0.35 + (now - this.wrapAt) / 160, 0, 1); rr(c, tx + hi * (cw + cg), ty, cw, ch, 8); c.fillStyle = ink; c.fill(); c.restore(); }
      c.textAlign = 'center';
      for (let i = 0; i < this.L; i++) { const k = this.tdd[i], on = hi != null && Math.abs(hi - i) < 0.5; c.font = font(13.5, on || k === 'U' ? 700 : 600); c.fillStyle = on ? card : k === 'U' ? D.cv('--ink3') : ink5; c.fillText(k, tx + i * (cw + cg) + cw / 2, ty + ch / 2 + 0.5); }
      c.textAlign = 'left';
      {
        const f1 = font(13.5, 500), f2 = font(13.5, 700);
        const parts = st === 'slots' && num(this.avg) ? [[narrow ? '평균 AI ' : '최근 ' + this.sc.P.length + ' 슬롯 평균 AI ', f1, ink4], [D.f(this.avg, 1) + ' SM', f2, ink2]]
          : st === 'mig' ? [['AI 고정 ', f1, ink4], [D.f(ai, 0) + ' SM', f2, ink2]] : st === 'tick' ? [['최근 1초 평균', f1, ink4]] : null;
        if (parts && runsW(c, parts) <= w - tx - tw - 14) runs(c, parts, w, ty + ch / 2 + 0.5, 'r');
      }
      c.globalAlpha = 1;
    }
  }

  // ---------------------------------------------------------------- PRB slicing: allocation = tinted block, use = solid UE segments
  // quota and use per service, use per UE - as the existing slicing bar reads the tick (views.js sliceData)
  function sliceData(t, m, cum) {
    const gg = (o, k) => !o ? null : cum ? (o.c ? o.c[k] : null) : o[k], tot = (m && m.prb_total) || 273, out = { tot };
    for (const s of ['va', 'nv']) {
      const ue = {}; let sum = 0, any = false, sv = t.svc ? t.svc[s] : null;
      for (const u of D.UE) if (u.svc === s) { const v = gg(t.ue ? t.ue[u.k] : null, 'prb'); ue[u.k] = v; if (num(v)) { sum += v; any = true; } }
      const use = gg(sv, 'prb'), q = gg(sv, 'quota');
      out[s] = { quota: num(q) ? q : null, use: num(use) ? use : (any ? sum : null), ue };
    }
    return out;
  }
  class PrbBar extends Stage {
    constructor(host, meta, subEl) {
      super(host); this.meta = meta; this.subEl = subEl; this.sd = null; this.noq = true; this.has = false; this.anchor = null; this.chip = null;
      this.dim = new Sp(1); this.q = { va: new Sp(null), nv: new Sp(null) }; this.use = { va: new Sp(null), nv: new Sp(null) }; this.ue = {};
      for (const u of D.UE) this.ue[u.k] = new Sp(null);
    }
    data(F) {
      super.data(F); const t = F.cur; this.sd = t && t.ue ? sliceData(t, F.meta, false) : null; const sd = this.sd;
      this.has = !!sd && (num(sd.va.use) || num(sd.nv.use) || num(sd.va.quota) || num(sd.nv.quota));
      this.noq = !sd || (!num(sd.va.quota) && !num(sd.nv.quota));
      if (this.subEl) D.txt(this.subEl, !this.has ? '서비스별 PRB 할당과 사용' : this.noq ? '슬라이싱 없음 (PF 공유) · 최근 1초' : '슬라이싱 적용 중 · 블록 = 할당, 채움 = 사용 · 최근 1초');
      const q = sd ? sd.va.quota : null;
      if (!num(q)) this.anchor = null;
      else if (this.anchor == null) this.anchor = q;
      else if (Math.abs(q - this.anchor) >= T.QUOTA_STEP) { this.chip = { d: q - this.anchor, at: performance.now() }; this.anchor = q; }   // the boundary moved
    }
    step(now, dt) {
      const sd = this.sd; let mv = false;
      const go = (s, v) => { if (num(v)) s.go(v, dt, T.W_BAR); else s.snap(null); if (s.moving(0.05)) mv = true; };
      if (this.has) { for (const s of ['va', 'nv']) { go(this.q[s], sd[s].quota); go(this.use[s], sd[s].use); for (const k in sd[s].ue) go(this.ue[k], sd[s].ue[k]); } }
      this.dim.go(this.has ? 0 : 1, dt, 10);
      if (this.chip && now - this.chip.at > T.CHIP_S * 1000 + 250) this.chip = null;
      return mv || this.dim.moving(0.01) || !!this.chip;
    }
    draw(now) {
      const c = this.begin(), w = this.w, sd = this.sd, tot = (sd && sd.tot) || 273, noq = this.noq, narrow = w < 700;
      const ink = D.cv('--ink'), ink2 = D.cv('--ink2'), ink3 = D.cv('--ink3'), ink4 = D.cv('--ink4'), ink5 = D.cv('--ink5'), card = D.cv('--card');
      c.globalAlpha = 1 - 0.5 * clamp(this.dim.x, 0, 1);
      const fL = font(15, 700), fN = font(34, 800), fU = font(15, 600), fS = font(14, 500), V = s => this.use[s].x, Q = s => this.q[s].x;
      // service name + numbers above each side
      const head = (s, right) => {
        const col = D.cv(D.SVC[s].color), name = s === 'va' ? 'VA 서비스' : 'Non-VA 서비스', u = V(s), q = Q(s);
        c.textBaseline = 'middle'; c.fillStyle = col; rr(c, right ? w - 12 : 0, 4, 12, 12, 4); c.fill();
        c.font = fL; c.fillStyle = ink2; c.textAlign = right ? 'right' : 'left'; c.fillText(name, right ? w - 19 : 19, 10.5); c.textAlign = 'left';
        c.textBaseline = 'alphabetic';
        const sN = u != null ? D.f(u, 0) : '–', ref = noq ? tot : q, extra = u != null && ref > 0 ? (noq ? '전체의 ' : '할당의 ') + D.f(100 * u / ref, 0) + ' %' : '';
        c.font = fU; const uw = c.measureText('PRB 사용').width, nw = tnumW(c, sN, fN), ew = extra && !narrow ? tnumW(c, extra, fS) + 12 : 0;
        let x = right ? w - (nw + 6 + uw + ew) : 0;
        tnum(c, sN, x, 58, fN, ink, 'l'); x += nw + 6; c.font = fU; c.fillStyle = ink4; c.fillText('PRB 사용', x, 58); x += uw + 12;
        if (ew) tnum(c, extra, x, 58, fS, ink5, 'l');
      };
      head('va', false); head('nv', true);
      const by = 94, bh = 48, R = 14, pad = 5, X = v => clamp(v / tot, 0, 1) * w;
      // solid UE segments inside [x0, x1], widths = scale x PRB
      const segs = (s, x0, x1, scale) => {
        const us = D.UE.filter(u => u.svc === s), any = us.some(u => this.ue[u.k].x != null); let x = x0;
        const list = any ? us.map(u => [this.ue[u.k].x, D.cv(u.color)]) : [[V(s), D.cv(D.SVC[s].color)]];
        for (const [v, col] of list) { if (!(v > 0)) continue; const sw = Math.min(v * scale, x1 - x); if (sw < 1) break; rr(c, x, by + pad, Math.max(1, sw - 2), bh - 2 * pad, Math.min(9, sw / 2)); c.fillStyle = col; c.fill(); x += sw; }
        return x;
      };
      c.textBaseline = 'middle';
      if (!this.has) { rr(c, 0, by, w, bh, R); c.fillStyle = D.cv('--hair'); c.fill(); pill(c, '대기 중', w / 2, 74, { align: 'c', bg: D.cv('--hair'), fg: ink4, mark: 'ring' }); }
      else if (noq) {
        // one shared pool: UE segments grouped by service, a dashed separator between the groups
        rr(c, 0, by, w, bh, R); c.fillStyle = D.cv('--hair'); c.fill();
        const sc = (w - 2 * pad - 14) / tot, xe = segs('va', pad, w - pad - 14, sc), xs = xe + 7;
        c.strokeStyle = ink5; c.lineWidth = 1.5; c.setLineDash([4, 4]); c.beginPath(); c.moveTo(xs, by - 7); c.lineTo(xs, by + bh + 7); c.stroke(); c.setLineDash([]);
        const xn = segs('nv', xs + 7, w - pad, sc);
        pill(c, '슬라이싱 없음 (PF 공유)', w / 2, 74, { align: 'c', bg: D.cv('--hair'), fg: ink3, h: 26, size: 13.5 });
        c.font = font(13, 500); c.fillStyle = ink5; c.textAlign = 'left'; c.fillText('0', 1, by + bh + 22); c.textAlign = 'right'; c.fillText('전체 ' + tot + ' PRB', w, by + bh + 22); c.textAlign = 'left';
        const free = tot - (V('va') || 0) - (V('nv') || 0), parts = [['여유 ', font(13, 500), ink5], [D.f(Math.max(0, free), 0) + ' PRB', font(13, 700), ink3]], tw = runsW(c, parts);
        if (w - pad - xn > tw + 24) runs(c, parts, (xn + w - pad) / 2, by + bh / 2 + 0.5, 'c');
      } else {
        // two blocks with a real gap; the boundary = divider + handle
        const qv = Q('va') || 0, qn = Q('nv') || 0, gap = 16, xb = clamp(X(qv), gap, w - gap), xe = clamp(X(qv + qn), xb + gap, w), rest = w - xe > 6;
        rr(c, 0, by, xb - gap / 2, bh, R); c.fillStyle = D.cv(D.SVC.va.weak); c.fill();
        rr(c, xb + gap / 2, by, (rest ? xe - gap / 2 : w) - xb - gap / 2, bh, R); c.fillStyle = D.cv(D.SVC.nv.weak); c.fill();
        if (rest) { rr(c, xe + gap / 2, by, w - xe - gap / 2, bh, R); c.fillStyle = D.cv('--hair'); c.fill(); }
        const wv = xb - gap / 2 - 2 * pad, wn = (rest ? xe - gap / 2 : w) - xb - gap / 2 - 2 * pad;
        if (qv > 0) segs('va', pad, pad + wv, wv / qv);
        if (qn > 0) segs('nv', xb + gap / 2 + pad, xb + gap / 2 + pad + wn, wn / qn);
        c.fillStyle = ink2; rr(c, xb - 1.25, by - 9, 2.5, bh + 18, 1.25); c.fill();
        rr(c, xb - 7, by + bh / 2 - 15, 14, 30, 7); c.fillStyle = card; c.fill(); c.lineWidth = 1.5; c.strokeStyle = ink2; c.stroke();
        c.fillStyle = ink4; c.fillRect(xb - 2.75, by + bh / 2 - 6, 1.5, 12); c.fillRect(xb + 1.25, by + bh / 2 - 6, 1.5, 12);
        // the boundary in numbers
        const f1 = font(13.5, 500), f2 = font(13.5, 700), sh = v => ' · ' + D.f(100 * v / tot, 0) + ' %';
        const L = [['VA 할당 ', f1, ink4], [D.f(qv, 0) + ' PRB', f2, ink2], [narrow ? '' : sh(qv), f1, ink5]], Rr = [['Non-VA 할당 ', f1, ink4], [D.f(qn, 0) + ' PRB', f2, ink2], [narrow ? '' : sh(qn), f1, ink5]];
        const lw = runsW(c, L), rw = runsW(c, Rr), ax = clamp(xb, lw + 12, w - rw - 12);
        runs(c, L, ax - 12, by + bh + 23, 'r'); runs(c, Rr, ax + 12, by + bh + 23, 'l');
        c.fillStyle = D.cv('--ink6'); c.fillRect(ax - 0.75, by + bh + 15, 1.5, 16);
        if (this.chip) {                                // the boundary just moved
          const a = (now - this.chip.at) / 1000, d = this.chip.d, k = a < T.CHIP_S ? 1 : clamp(1 - (a - T.CHIP_S) / 0.25, 0, 1);
          const txt = 'VA 할당 ' + (d > 0 ? '+' : '−') + D.f(Math.abs(d), 0) + ' PRB', f = font(13.5, 700); c.font = f; const pw = c.measureText(txt).width + 26;
          pill(c, txt, clamp(xb, pw / 2, w - pw / 2), 74, { align: 'c', bg: ink, fg: card, h: 26, size: 13.5, px: 13, alpha: k, scale: 0.75 + 0.25 * backOut(a / 0.3) });
        }
      }
      // the UEs: colour key with the PRB of each
      {
        const f1 = font(13.5, 500), f2 = font(13.5, 700), y = by + bh + 56; let x = 0; c.textBaseline = 'middle';
        for (const u of D.UE) {
          const v = this.ue[u.k].x, parts = [[(narrow ? u.name.replace('Non-VA', 'NV') : u.name) + ' ', f1, ink4], [v != null ? D.f(v, 0) : '–', f2, ink2]], tw = runsW(c, parts);
          if (x + 18 + tw > w) break;
          c.fillStyle = D.cv(u.color); rr(c, x, y - 6, 12, 12, 4); c.fill(); runs(c, parts, x + 18, y + 0.5, 'l'); x += 18 + tw + (narrow ? 14 : 26);
        }
        if (x + 30 < w) { c.font = f1; c.fillStyle = ink5; c.fillText('PRB', x - (narrow ? 8 : 18), y + 0.5); }
      }
      c.globalAlpha = 1;
    }
  }

  // ---------------------------------------------------------------- time charts: the cumulative mean is the hero line
  /* opt = { unit, digits, series: [{label, color, cum: tick => v, inst: tick => v}], thresholds: [{label, get: (meta, tick) => v, ref}],
             yFixed, yStep, fit (a y window around the data instead of 0-based), followSel (누적/순간 as the existing charts), limit: meta => v }
     The x axis and the 전체|5분|1분 window are those of the existing charts (D.frame). While a measurement runs the hero line is the cumulative
     mean; the last-1-s value is a faint trace behind it unless the page selector says "누적 평균". Outside a measurement there is no cumulative
     mean: the last-1-s value is drawn, as the existing charts do. */
  const TSTEPS = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
  const ease3 = k => 1 - Math.pow(1 - clamp(k, 0, 1), 3);
  class Chart extends Stage {
    constructor(host, opt) {
      super(host); this.opt = opt; this.tip = el('div.tip'); host.appendChild(this.tip); this.mx = null; this.my = null;
      this.lastT = null; this.newAt = -1e9; this.ends = null; this.from = null; this.seenT = null; this.arr = -1e9; this.due = 0; this.lag = opt.lag || 0;
      this.cvs.addEventListener('mousemove', e => { const r = this.cvs.getBoundingClientRect(); this.mx = e.clientX - r.left; this.my = e.clientY - r.top; this.dirty = true; });
      this.cvs.addEventListener('mouseleave', () => { this.mx = null; this.tip.style.display = 'none'; this.dirty = true; });
    }
    // A new point animates for a quarter of a second, and the charts of the tab take turns (opt.lag, a sixth of a second apart): six charts that all
    // redraw at once, or all the time, would take the Capture paint budget away from the meters and the SM bar. Only the steady 1-s cadence waits
    // for its turn; a seek, a faster replay or anything that asks for a redraw is drawn at once.
    data(F) {
      const cur = F && F.cur, now = performance.now();
      if (cur && cur.t !== this.seenT) {
        const steady = this.lag > 0 && !this.due && now - this.arr > 800 && now - this.arr < 1500;
        this.seenT = cur.t; this.arr = now;
        if (steady) { this.F = F; this.due = now + this.lag; return; }
        this.due = 0; this.mark(cur.t, now);
      }
      super.data(F);
    }
    mark(t, now) { this.from = this.ends; this.fromT = this.lastT; this.lastT = t; this.newAt = now; }   // a new point: the line end grows to it
    step(now) {
      if (this.due && now >= this.due) { this.due = 0; if (this.F && this.F.cur) this.mark(this.F.cur.t, now); this.dirty = true; }
      return now - this.newAt < 270;
    }
    empty(c, msg) { if (this.opt.stat) this.drawStat(c, null, null); c.fillStyle = D.cv('--ink5'); c.font = font(14, 500); c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(msg, this.w / 2, this.h / 2); c.textAlign = 'left'; this.tip.style.display = 'none'; }
    // opt.stat: one figure in the top right corner of the picture, relative to a base value: "<label> 99.4 %" and the difference beside it, smaller.
    // v is the hero line's end value, so the figure moves with the line end (same tween). Rounded once: the two figures always add up to 100.
    statOf(v, base) { if (!(num(v) && base > 0)) return null; const r = Math.round(1000 * v / base) / 10; return { r, d: Math.round((100 - r) * 10) / 10 }; }
    drawStat(c, v, base) {                              // -> left edge of what was drawn
      const st = this.opt.stat, q = this.statOf(v, base), by = 33; let x = this.w;
      c.textBaseline = 'alphabetic';
      if (q) { x -= tnum(c, (q.d >= 0 ? st.loss : st.gain) + ' ' + D.f(Math.abs(q.d), 1) + ' %', x, by - 1, font(14, 600), D.cv('--ink4'), 'r'); x -= 16; }
      if (q) { c.font = font(16, 700); c.fillStyle = D.cv('--ink3'); c.textAlign = 'right'; c.fillText('%', x, by - 1); x -= c.measureText('%').width + 4; }
      x -= tnum(c, q ? D.f(q.r, 1) : '–', x, by, font(28, 800), q ? D.cv('--ink') : D.cv('--ink5'), 'r'); x -= 10;
      c.font = font(14, 600); c.fillStyle = D.cv('--ink3'); c.textAlign = 'right'; c.fillText(st.label, x, by - 1); x -= c.measureText(st.label).width;
      c.textAlign = 'left'; c.textBaseline = 'middle'; return x;
    }
    showTip(x, y, title, rows) {
      const t = this.tip; t.textContent = ''; t.appendChild(el('div.tt', title));
      for (const r of rows) { const i = el('i'); if (r[0]) i.style.background = r[0]; else i.style.visibility = 'hidden'; t.appendChild(el('div.tr', i, el('span', r[1]), el('b', r[2]))); }
      t.style.display = 'block';
      const tw = t.offsetWidth, th = t.offsetHeight;
      let left = x + 14; if (left + tw > this.w - 4) left = x - 14 - tw; if (left < 4) left = 4;
      let top = y - th / 2; if (top < 2) top = 2; if (top + th > this.h - 2) top = Math.max(2, this.h - 2 - th);
      t.style.left = left + 'px'; t.style.top = top + 'px';
    }
    draw(now) {
      const F = this.F, c = this.begin(), o = this.opt, w = this.w, h = this.h;
      if (!F || !F.cur || F.n - F.i0 < 1) return this.empty(c, '데이터 수신 대기 중');
      const ticks = F.ticks, n = F.n, i0 = F.i0, cur = F.cur, meta = F.meta || {}, unit = o.unit || '', dg = o.digits == null ? 1 : o.digits;
      const hasCum = !!cur.rec, mode = D.S.val, heroCum = o.followSel ? (hasCum && mode !== 'inst') : hasCum, trace = heroCum && mode !== 'cum';
      const ink2 = D.cv('--ink2'), ink3 = D.cv('--ink3'), ink4 = D.cv('--ink4'), ink5 = D.cv('--ink5'), card = D.cv('--card');
      const st = o.stat || null, x0 = 52, x1 = w - 92, y0 = st ? 56 : 42, y1 = h - 30, ry = st ? 24 : 14;
      const tA = ticks[i0].t, tB = Math.max(cur.t, tA + 60), X = t => x0 + (t - tA) / (tB - tA) * (x1 - x0);
      const stp = Math.max(1, Math.floor((n - i0) / Math.max(50, (x1 - x0) / 1.5)));
      const val = (fn, tk) => { let v; try { v = fn(tk); } catch (e) { v = null; } return num(v) ? v : null; };
      let vmax = -Infinity, vmin = Infinity;
      const lines = o.series.map(s => {
        const hf = heroCum ? s.cum : s.inst, hp = [], tp = []; let last = null, lastT = null;
        for (let i = i0; i < n; i++) {
          if (i % stp && i !== n - 1) continue;
          const tk = ticks[i], v = val(hf, tk); hp.push([tk.t, v]);
          if (v != null) { last = v; lastT = tk.t; if (v > vmax) vmax = v; if (v < vmin) vmin = v; }
          if (trace) { const q = val(s.inst, tk); tp.push([tk.t, q]); if (q != null) { if (q > vmax) vmax = q; if (q < vmin) vmin = q; } }
        }
        return { s, hp, tp, last, lastT, col: D.cv(s.color) };
      });
      if (!(vmax > -Infinity)) { this.ends = null; return this.empty(c, o.emptyMsg || '이 구간에 데이터가 없습니다'); }
      const ths = [];
      for (const th of (o.thresholds || [])) { const v = val(tk => th.get(meta, tk), cur); if (v != null) { ths.push([th, v]); if (v > vmax) vmax = v; if (v < vmin) vmin = v; } }
      // y scale
      let lo = 0, sc;
      if (o.yFixed) sc = { step: o.yStep || o.yFixed / 4, max: o.yFixed };
      else if (o.fit) { const span = Math.max(vmax - vmin, o.fitMin || 2), a = vmin - span * 0.25, b = vmax + span * 0.25, st = nice(b - a, 4).step; lo = Math.max(0, Math.floor(a / st) * st); sc = { step: st, max: Math.ceil(b / st) * st }; }
      else sc = nice(Math.max(vmax * 1.1, o.yFloor || 0), 4);
      const Y = v => y1 - clamp((v - lo) / (sc.max - lo), -0.02, 1.03) * (y1 - y0);
      // grid + axis labels (recessive)
      c.font = font(13, 500); c.textBaseline = 'middle'; c.textAlign = 'right'; c.lineWidth = 1;
      const r6 = v => Math.round(v * 1e6) / 1e6, ydg = Number.isInteger(r6(sc.step)) ? 0 : Number.isInteger(r6(sc.step * 10)) ? 1 : 2;
      for (let v = lo; v <= sc.max + 1e-9; v += sc.step) {
        const y = Math.round(Y(v)) + 0.5;
        c.strokeStyle = D.cv(v === lo && !o.fit ? '--line' : '--hair'); c.beginPath(); c.moveTo(x0, y); c.lineTo(x1, y); c.stroke();
        c.fillStyle = ink5; c.fillText(D.f(v, ydg), x0 - 9, y);
      }
      const span = tB - tA; let ts = TSTEPS[TSTEPS.length - 1];
      for (const s of TSTEPS) if (span / s <= 6.5) { ts = s; break; }
      const base = hasCum ? cur.t - (cur.el || 0) : 0;
      c.textAlign = 'center'; c.textBaseline = 'top'; c.fillStyle = ink5;
      for (let t = Math.ceil((tA - base) / ts - 1e-6) * ts + base; t <= tB + 1e-6; t += ts) {
        const x = X(t); if (x < x0 - 1 || x > x1 + 1) continue;
        c.fillText(hasCum ? D.mmss(t - base) : D.clock(t, ts < 60), clamp(x, x0 + 18, x1 - 16), y1 + 9);
      }
      // the top row: unit, series, what the lines are
      // the newest point grows out of the previous one
      const k = ease3((now - this.newAt) / 240), prevT = n >= 2 ? ticks[n - 2].t : null;
      const ends = lines.map((L, i) => {
        const fr = this.from && this.from[i], tw = k < 1 && fr && fr.v != null && L.last != null && L.lastT === cur.t && fr.t === prevT && this.fromT === prevT;
        return L.last == null ? null : tw ? { t: fr.t + (L.lastT - fr.t) * k, v: fr.v + (L.last - fr.v) * k, tw: true } : { t: L.lastT, v: L.last };
      });
      const sx = st ? this.drawStat(c, ends[0] ? ends[0].v : null, val(tk => st.base(meta, tk), cur)) : null;
      c.textBaseline = 'middle'; c.textAlign = 'right'; c.fillStyle = D.cv('--ink6'); c.font = font(13, 500); c.fillText(unit, x0 - 9, ry); c.textAlign = 'left';
      let lx = x0;
      if (o.series.length > 1) for (const L of lines) { c.fillStyle = L.col; rr(c, lx, ry - 2, 16, 4, 2); c.fill(); c.font = font(13.5, 600); c.fillStyle = ink3; c.fillText(L.s.label, lx + 22, ry + 0.5); lx += 22 + c.measureText(L.s.label).width + 18; }
      {
        const kc = o.series.length > 1 ? ink4 : lines[0].col, f = font(13, 500), items = [[heroCum ? '누적 평균' : '최근 1초', 1]];
        if (trace) items.push(['최근 1초', 0]);
        c.font = f; let kw = 0; for (const it of items) kw += 22 + c.measureText(it[0]).width + 14; kw -= 14;
        const note = hasCum ? '' : '측정 시작 후 누적 평균 표시', nw = note ? c.measureText(note).width + 14 : 0;
        const rx = sx != null ? sx - 20 : x1, fits = rx - lx >= kw, withNote = note && rx - lx >= kw + nw; let kx = sx != null ? lx : x1 - kw;   // beside a stat the key starts on the left
        if (fits) {
          if (withNote && sx == null) { c.fillStyle = ink5; c.textAlign = 'right'; c.fillText(note, kx - 14, ry + 0.5); c.textAlign = 'left'; }
          for (const it of items) { c.fillStyle = it[1] ? kc : rgba(kc, 0.5); if (it[1]) rr(c, kx, ry - 1.5, 16, 3, 1.5); else rr(c, kx, ry - 0.75, 16, 1.5, 0.75); c.fill(); c.fillStyle = ink4; c.font = f; c.fillText(it[0], kx + 22, ry + 0.5); kx += 22 + c.measureText(it[0]).width + 14; }
          if (withNote && sx != null) { c.fillStyle = ink5; c.font = f; c.fillText(note, kx, ry + 0.5); }
        }
      }
      c.save(); c.beginPath(); c.rect(x0 - 2, y0 - 8, x1 - x0 + 4, y1 - y0 + 10); c.clip();
      for (const [th, v] of ths) {                      // thresholds first, data on top
        const y = Math.round(Y(v)) + 0.5; c.strokeStyle = D.cv(th.ref ? '--ink6' : '--bad'); c.lineWidth = 1.5; c.setLineDash([6, 5]); c.beginPath(); c.moveTo(x0, y); c.lineTo(x1, y); c.stroke(); c.setLineDash([]);
      }
      c.lineJoin = 'round'; c.lineCap = 'round';
      const path = (pts, end) => {                      // -> [first x, last x] of the last run, null = nothing drawn
        c.beginPath(); let pen = false, fx = null, lx2 = null; const m = pts.length;
        for (let j = 0; j < m; j++) {
          let t = pts[j][0], v = pts[j][1]; if (v == null) { pen = false; continue; }
          if (end && end.tw && j === m - 1) { t = end.t; v = end.v; }
          const x = X(t), y = Y(v); if (pen) c.lineTo(x, y); else { c.moveTo(x, y); pen = true; fx = x; } lx2 = x;
        }
        return fx == null ? null : [fx, lx2];
      };
      lines.forEach((L, i) => {
        if (trace) { path(L.tp, null); c.strokeStyle = rgba(L.col, 0.42); c.lineWidth = 1.25; c.stroke(); }
        if (lines.length === 1 && !o.fit) {             // one series: a soft wash under the hero line (gaps would need one wash per run: the last run only)
          const r = path(L.hp, ends[i]);
          if (r && r[1] > r[0]) { c.lineTo(r[1], y1); c.lineTo(r[0], y1); c.closePath(); const gr = c.createLinearGradient(0, y0, 0, y1); gr.addColorStop(0, rgba(L.col, 0.2)); gr.addColorStop(1, rgba(L.col, 0.02)); c.fillStyle = gr; c.fill(); }
        }
      });
      lines.forEach((L, i) => { path(L.hp, ends[i]); c.strokeStyle = L.col; c.lineWidth = 3; c.stroke(); });
      c.restore();
      // threshold labels
      for (const [th, v] of ths) {
        const y = Y(v), txt = th.label + ' ' + D.f(v, th.digits == null ? 0 : th.digits) + (unit && !th.noUnit ? ' ' + unit : '');
        c.font = font(13, 600); const tw = c.measureText(txt).width, below = y - 22 < y0 - 6, ly = below ? y + 12 : y - 11;
        c.fillStyle = rgba(card, 0.9); rr(c, x0 + 6, ly - 9, tw + 12, 18, 6); c.fill();
        c.fillStyle = th.ref ? ink4 : ink3; c.textAlign = 'left'; c.textBaseline = 'middle'; c.fillText(txt, x0 + 12, ly + 0.5);
      }
      // line ends: halo on a new point, dot with a surface ring, the value riding beside it
      const hk = (now - this.newAt) / 260, lab = [];
      lines.forEach((L, i) => {
        const e = ends[i]; if (!e) return; const x = X(e.t), y = Y(e.v);
        if (hk >= 0 && hk < 1 && L.lastT === cur.t) { c.beginPath(); c.arc(x, y, 6 + 11 * ease3(hk), 0, TAU); c.fillStyle = rgba(L.col, 0.3 * (1 - hk)); c.fill(); }
        c.beginPath(); c.arc(x, y, 10.5, 0, TAU); c.fillStyle = rgba(L.col, 0.16); c.fill();
        c.beginPath(); c.arc(x, y, 6.5, 0, TAU); c.fillStyle = card; c.fill(); c.beginPath(); c.arc(x, y, 4.5, 0, TAU); c.fillStyle = L.col; c.fill();
        lab.push({ L, v: e.v, y, ly: y });
      });
      lab.sort((a, b) => a.y - b.y);
      for (let i = 1; i < lab.length; i++) if (lab[i].ly < lab[i - 1].ly + 20) lab[i].ly = lab[i - 1].ly + 20;
      for (let i = lab.length - 1; i >= 0; i--) { const lim = i === lab.length - 1 ? y1 + 2 : lab[i + 1].ly - 20; if (lab[i].ly > lim) lab[i].ly = lim; }
      const lim = o.limit ? val(tk => o.limit(meta, tk), cur) : null;
      c.textBaseline = 'middle';
      for (const e of lab) {
        const over = lim != null && e.v > lim, nw = tnum(c, D.f(e.v, dg), x1 + 13, e.ly + 0.5, font(15, 800), over ? D.cv('--bad-ink') : D.cv('--ink'), 'l');
        if (unit) { c.font = font(13, 500); c.fillStyle = ink5; c.fillText(unit, x1 + 13 + nw + 4, e.ly + 1); }
      }
      this.ends = lines.map(L => ({ t: L.lastT, v: L.last }));
      // crosshair + tooltip
      if (this.mx != null && this.mx >= x0 - 4 && this.mx <= x1 + 4) {
        const t = tA + (this.mx - x0) / (x1 - x0) * (tB - tA); let a = i0, b = n - 1;
        while (a < b) { const m = (a + b) >> 1; if (ticks[m].t < t) a = m + 1; else b = m; }
        if (a > i0 && Math.abs(ticks[a - 1].t - t) < Math.abs(ticks[a].t - t)) a--;
        const tk = ticks[a], x = Math.round(X(tk.t)) + 0.5, rows = [];
        c.strokeStyle = D.cv('--ink6'); c.lineWidth = 1; c.beginPath(); c.moveTo(x, y0 - 4); c.lineTo(x, y1); c.stroke();
        for (const L of lines) {
          const vi = val(L.s.inst, tk), vc = tk.rec ? val(L.s.cum, tk) : null, hv = heroCum ? vc : vi;
          rows.push([L.col, L.s.label, vc != null && vi != null ? '누적 ' + D.f(vc, dg) + ' · 순간 ' + D.f(vi, dg) + (unit ? ' ' + unit : '') : D.fu(vc != null ? vc : vi, dg, unit)]);
          if (hv != null) { c.beginPath(); c.arc(x, Y(hv), 6, 0, TAU); c.fillStyle = card; c.fill(); c.beginPath(); c.arc(x, Y(hv), 4, 0, TAU); c.fillStyle = L.col; c.fill(); }
        }
        for (const [th, v] of ths) rows.push([null, th.label, D.fu(v, th.digits == null ? 0 : th.digits, th.noUnit ? '' : unit)]);
        if (st) {                                       // the same two figures for the point under the pointer
          const q = this.statOf(heroCum ? (tk.rec ? val(lines[0].s.cum, tk) : null) : val(lines[0].s.inst, tk), val(t2 => st.base(meta, t2), tk));
          if (q) { rows.push([null, st.label, D.f(q.r, 1) + ' %']); rows.push([null, q.d >= 0 ? st.loss : st.gain, D.f(Math.abs(q.d), 1) + ' %']); }
        }
        this.showTip(x, this.my == null ? y0 : this.my, tk.rec ? '측정 ' + D.mmss(tk.el) + '  ·  ' + D.clock(tk.t) : D.clock(tk.t), rows);
      } else this.tip.style.display = 'none';
    }
  }

  // ---------------------------------------------------------------- the tab
  let V = null;
  const modeOf = (t, m) => (t && t.sys && t.sys.mode) || (m && m.mode);
  D.views = D.views || {};
  D.views.demo = {
    build(host, meta) {
      const m = meta || {}, v = V = { all: [], meta: m }, grid = () => { const e = el('div.grid'); host.appendChild(e); return e; };
      // 1. the four UEs: momentary latency of every frame against the QoS deadline
      const g1 = grid();
      for (const u of D.UE) {
        const va = u.svc === 'va', card = D.card('s6', u.name, va ? 'E2E Latency · 캡처부터 결과 표시까지' : 'E2E Latency (업로드 완료까지) · 폰이 보내기 시작해 서버가 다 받을 때까지');
        const body = el('div.dm-body'); card.el.classList.add('dm-card', 'dm-meter'); card.add(body); g1.appendChild(card.el);
        v.all.push(new Meter(card.el, body, u, m));
      }
      // 2. the two resources that are split: GPU SMs (RAN | AI), PRBs (VA | Non-VA)
      const g2 = grid(), bar = (title, sub) => { const card = D.card('s6', title, sub), h = el('div.dm-stage.dm-h-bar'); card.el.classList.add('dm-card'); card.add(h); g2.appendChild(card.el); return { card, h }; };
      const b1 = bar('GPU SM 배분', ''), b2 = bar('PRB 슬라이싱', '서비스별 PRB 할당과 사용');
      v.smSub = b1.card.subEl; v.sm = new SmBar(b1.h, m); v.prb = new PrbBar(b2.h, m, b2.card.subEl); v.all.push(v.sm, v.prb);
      // 3. the six charts
      let gc = null, nch = 0;
      const chart = (title, sub, opt, note) => {
        if (!gc || gc.childElementCount >= 2) gc = grid();
        const card = D.card('s6', title, sub), h = el('div.dm-stage.dm-h-chart'); card.el.classList.add('dm-card'); card.add(h); if (note) card.add(el('div.dm-note', note));
        gc.appendChild(card.el); const ch = new Chart(h, Object.assign({ lag: (nch++ % 6) * 1000 / 6 }, opt)); v.all.push(ch); return card;
      };
      const ysm = num((m.baseline || {}).yolo_sm) ? m.baseline.yolo_sm : 60, bm = m.base_model || '26x', isBase = t => modeOf(t, m) === 'baseline';
      chart('VA 평균 E2E Latency', '캡처부터 결과 표시까지 · 측정 시작부터의 누적 평균', { unit: 'ms', digits: 1, limit: mm => mm.deadline_ms || 200,
        series: [{ label: 'VA', color: '--c1', cum: t => g(t, 'svc.va.c.e2e'), inst: t => g(t, 'svc.va.e2e') }], thresholds: [{ label: 'QoS', get: mm => mm.deadline_ms || 200 }] });
      chart('Non-VA 평균 E2E Latency', '업로드 완료까지 · 측정 시작부터의 누적 평균', { unit: 'ms', digits: 1, limit: mm => mm.nv_deadline_ms || 250,
        series: [{ label: 'Non-VA', color: '--c3', cum: t => g(t, 'svc.nv.c.ul'), inst: t => g(t, 'svc.nv.ul') }], thresholds: [{ label: 'QoS', get: mm => mm.nv_deadline_ms || 250 }] });
      chart('평균 정확도', '평균 mAP (요청 가중) · 측정 시작부터의 누적 평균', { unit: 'mAP', digits: 2, fit: true, fitMin: 2,
        stat: { label: 'Baseline 대비', loss: '감소', gain: '증가', base: mm => (mm.map || {})[mm.base_model || '26x'] },
        series: [{ label: '평균 mAP', color: '--c1', cum: t => g(t, 'gpu.c.map'), inst: t => g(t, 'gpu.map') }],
        thresholds: [{ label: bm + ' 고정 Baseline', digits: 1, ref: true, noUnit: true, get: mm => (mm.map || {})[mm.base_model || '26x'] }] },
        m.map_src ? '모델별 mAP = ' + m.map_src + '. 우리 FHD 파이프라인에서 실측한 값이 아닙니다.' : '');
      v.smChart = chart('AI 평균 SM', '', { unit: 'SM', digits: 1, yFixed: NSM, yStep: 33,
        series: [{ label: 'AI', color: '--c1', cum: t => isBase(t) ? (t.rec ? ysm : null) : g(t, 'gpu.c.sm.free'), inst: t => isBase(t) ? ysm : g(t, 'gpu.sm.free') }] });
      chart('서비스별 PRB 사용', '슬롯당 PRB', { unit: 'PRB', digits: 1, followSel: true,
        series: [{ label: 'VA 사용', color: '--c1', cum: t => g(t, 'svc.va.c.prb'), inst: t => g(t, 'svc.va.prb') }, { label: 'Non-VA 사용', color: '--c3', cum: t => g(t, 'svc.nv.c.prb'), inst: t => g(t, 'svc.nv.prb') }] });
      chart('서비스별 평균 QoS 충족률', 'QoS 기한 안에 끝난 프레임의 비율 · 측정 시작부터의 누적', { unit: '%', digits: 1, yFixed: 100, yStep: 25,
        series: [{ label: 'VA', color: '--c1', cum: t => g(t, 'svc.va.c.qos'), inst: t => g(t, 'svc.va.qos') }, { label: 'Non-VA', color: '--c3', cum: t => g(t, 'svc.nv.c.qos'), inst: t => g(t, 'svc.nv.qos') }] });
      v.ysm = ysm; kick();
    },
    update(F) {
      const v = V; if (!v) return;
      const base = modeOf(F.cur, F.meta) === 'baseline';
      D.txt(v.smSub, base ? 'Baseline · MIG 고정 분할 · 전체 ' + NSM + ' SM' : 'FAPI-Aware Scheduler가 슬롯마다 정하는 배분 · 전체 ' + NSM + ' SM');
      D.txt(v.smChart.subEl, base ? 'MIG 고정 ' + v.ysm + ' SM · 전체 ' + NSM + ' SM 중' : 'YOLO에 제공된 SM의 슬롯 평균 · 전체 ' + NSM + ' SM 중 · 누적 평균');
      for (const s of v.all) if (!s.dead) s.data(F);
    },
    scope(F) { if (V && V.sm && !V.sm.dead) V.sm.scope(F); },
  };
  // the per-frame feed of the live stream (app.js onMsg, message kind "fr")
  D.demo = { fr(m) { try { Feed.live(m); } catch (e) { /* a malformed message must not stop the page */ } }, view: () => V, T };
})();
