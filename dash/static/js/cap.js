/* _dgist experiment dashboard - Capture (2026-11-14): the graphs / figures picked in a checklist and the four viewer screens, each as its own
   MP4 file, made in THIS browser and downloaded on this PC. Nothing is stored on or sent to the server; no UE is involved.
   - One worker holds every recorder (WebCodecs H.264 + the MP4 writer of the /viewer page). All recorders share one start instant and one
     frame grid and end on the same frame, so every file has the same length and frame k of each file is the same moment.
   - A graph / figure is painted from the page's own DOM (computed styles, live boxes, the chart canvases) at the size it has on the page
     times the screen's pixel ratio, so transitions and number glides are recorded as they run. Not reproduced: hover effects.
   - Works for the live view and for Replay (the files then follow the replay as it is shown). The viewer screens are live video only.
   - WebCodecs needs a secure origin: Capture runs on the dashboard's https:// address (meta.tls_port). */
'use strict';
(function () {
  const D = window.D, el = D.el, S = D.S, $ = D.$;

  // ------------------------------------------------------------------ MP4 writer + recorders: the source of the worker
  function mp4Str(s) { const a = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) a[i] = s.charCodeAt(i); return a; }
  function mp4U32() { const a = new Uint8Array(4 * arguments.length), v = new DataView(a.buffer); for (let i = 0; i < arguments.length; i++) v.setUint32(4 * i, arguments[i] >>> 0); return a; }
  function mp4U16() { const a = new Uint8Array(2 * arguments.length), v = new DataView(a.buffer); for (let i = 0; i < arguments.length; i++) v.setUint16(2 * i, arguments[i]); return a; }
  function mp4Box(type) {   // size, type, then the byte arrays given
    let n = 8; for (let i = 1; i < arguments.length; i++) n += arguments[i].length;
    const a = new Uint8Array(n); new DataView(a.buffer).setUint32(0, n); a.set(mp4Str(type), 4);
    let o = 8; for (let i = 1; i < arguments.length; i++) { a.set(arguments[i], o); o += arguments[i].length; }
    return a;
  }
  function mp4Table(head, vals, wide) {   // version/flags 0, the head words, then one 32-bit (wide: 64-bit) word per value
    const w = wide ? 8 : 4, a = new Uint8Array(4 + 4 * head.length + w * vals.length), v = new DataView(a.buffer);
    let o = 4; for (const h of head) { v.setUint32(o, h); o += 4; }
    for (const x of vals) { if (wide) { v.setUint32(o, Math.floor(x / 4294967296)); v.setUint32(o + 4, x >>> 0); } else v.setUint32(o, x); o += w; }
    return a;
  }
  function mp4Moov(r, dataAt, wide) {   // r: w, h, sizes, durs (TS ticks), keys (sample numbers from 1), desc (avcC payload), color | dataAt: file offset of the first sample
    const n = r.sizes.length, now = Math.floor(Date.now() / 1000) + 2082844800, unity = mp4U32(0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000);
    let total = 0; const runs = [];
    for (const d of r.durs) { total += d; if (runs.length && runs[runs.length - 1] === d) runs[runs.length - 2]++; else runs.push(1, d); }
    const offs = new Array(n); let o = dataAt; for (let i = 0; i < n; i++) { offs[i] = o; o += r.sizes[i]; }
    const c = r.color, colr = c ? [mp4Box('colr', mp4Str('nclx'), mp4U16(c[0], c[1], c[2]), new Uint8Array([c[3] ? 0x80 : 0]))] : [];
    const avc1 = mp4Box('avc1', new Uint8Array(6), mp4U16(1, 0, 0), mp4U32(0, 0, 0), mp4U16(r.w, r.h), mp4U32(0x480000, 0x480000, 0), mp4U16(1), new Uint8Array(32), mp4U16(0x18, 0xffff),
                        mp4Box('avcC', r.desc), ...colr, mp4Box('pasp', mp4U32(1, 1)));
    const stbl = mp4Box('stbl', mp4Box('stsd', mp4U32(0, 1), avc1), mp4Box('stts', mp4Table([runs.length / 2], runs)), mp4Box('stss', mp4Table([r.keys.length], r.keys)),
                        mp4Box('stsc', mp4U32(0, 1, 1, 1, 1)), mp4Box('stsz', mp4Table([0, n], r.sizes)), mp4Box(wide ? 'co64' : 'stco', mp4Table([n], offs, wide)));
    const minf = mp4Box('minf', mp4Box('vmhd', mp4U32(1), mp4U16(0, 0, 0, 0)), mp4Box('dinf', mp4Box('dref', mp4U32(0, 1), mp4Box('url ', mp4U32(1)))), stbl);
    const mdia = mp4Box('mdia', mp4Box('mdhd', mp4U32(0, now, now, TS, total), mp4U16(0x55c4, 0)),
                        mp4Box('hdlr', mp4U32(0, 0), mp4Str('vide'), mp4U32(0, 0, 0), mp4Str('VideoHandler'), new Uint8Array(1)), minf);
    const trak = mp4Box('trak', mp4Box('tkhd', mp4U32(3, now, now, 1, 0, total, 0, 0), mp4U16(0, 0, 0, 0), unity, mp4U32(r.w << 16, r.h << 16)), mdia);
    return mp4Box('moov', mp4Box('mvhd', mp4U32(0, now, now, TS, total, 0x10000), mp4U16(0x100, 0), mp4U32(0, 0), unity, mp4U32(0, 0, 0, 0, 0, 0, 2)), trak);
  }
  function mp4File(r) {   // -> Blob; 64-bit chunk offsets and mdat size only past 4 GB
    let bytes = 0; for (const s of r.sizes) bytes += s;
    const ftyp = mp4Box('ftyp', mp4Str('isom'), mp4U32(0x200), mp4Str('isomiso2avc1mp41'));
    const wide = bytes + 16 * r.sizes.length + 65536 > 0xffffffff, head = wide ? 16 : 8;
    const moov = mp4Moov(r, ftyp.length + mp4Moov(r, 0, wide).length + head, wide);
    const mdat = new Uint8Array(head), v = new DataView(mdat.buffer); mdat.set(mp4Str('mdat'), 4);
    if (wide) { v.setUint32(0, 1); v.setUint32(8, Math.floor((bytes + 16) / 4294967296)); v.setUint32(12, (bytes + 16) >>> 0); } else v.setUint32(0, bytes + 8);
    return new Blob([ftyp, moov, mdat].concat(r.parts), { type: 'video/mp4' });
  }
  function roundRect(g, x, y, w, h, r) {
    g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r); g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
  }
  function recPicture(g, src, dim, W, H) {   // a viewer screen's picture fitted into W x H; dim = the screen shows it frozen
    const k = Math.min(W / src.width, H / src.height), w = Math.round(src.width * k), h = Math.round(src.height * k), x = (W - w) >> 1, y = (H - h) >> 1;
    g.fillStyle = '#000'; g.fillRect(0, 0, W, H);
    const filter = dim && typeof g.filter === 'string';
    if (filter) g.filter = 'saturate(.7) brightness(.8)';
    g.drawImage(src, x, y, w, h);
    if (filter) g.filter = 'none'; else if (dim) { g.fillStyle = 'rgba(0,0,0,.2)'; g.fillRect(x, y, w, h); }
  }
  function recLayer(g, ov, font, W, H) {   // the badge at twice its size, the waiting text
    if (ov.badge) {
      const a = ov.badge.a, b = ov.badge.b, fa = '700 30px ' + font, fb = '500 24px ' + font;
      g.font = fa; const wa = g.measureText(a).width; g.font = fb; const wb = b ? g.measureText(b).width : 0;
      g.fillStyle = ov.badge.grey ? 'rgba(31,42,54,.92)' : 'rgba(255,77,94,.92)'; roundRect(g, 24, 24, Math.max(wa, wb) + 48, b ? 104 : 68, 20); g.fill();
      g.fillStyle = ov.badge.grey ? '#e6edf3' : '#fff'; g.textBaseline = 'middle'; g.font = fa; g.fillText(a, 48, 59);
      if (b) { g.globalAlpha = .92; g.font = fb; g.fillText(b, 48, 99); g.globalAlpha = 1; }
    }
    if (ov.wait) {
      g.fillStyle = 'rgba(11,15,20,.6)'; g.fillRect(0, 0, W, H);
      g.fillStyle = '#8b98a5'; g.font = '400 30px ' + font; g.textBaseline = 'middle'; g.textAlign = 'center'; g.fillText(ov.wait, W / 2, H / 2); g.textAlign = 'left';
    }
  }
  function capWorker() {
    const recs = new Map(), now = () => performance.timeOrigin + performance.now();
    let t0 = 0, fps = 30, font = 'sans-serif', maxSlots = 0, stopSlot = -1, timer = 0, started = false, ended = false, statAt = 0, maxed = false;
    const fail = (r, e) => { if (!r.error) r.error = String((e && e.message) || e); };
    const group = r => { if (r.curBytes) { r.parts.push(new Blob(r.cur)); r.cur = []; r.curBytes = 0; } };   // one Blob per key-frame group
    const color = cs => { const P = { bt709: 1, bt470bg: 5, smpte170m: 6 }, T = { bt709: 1, smpte170m: 6, 'iec61966-2-1': 13 }, M = { rgb: 0, bt709: 1, bt470bg: 5, smpte170m: 6 };
      return (cs && cs.primaries in P && cs.transfer in T && cs.matrix in M) ? [P[cs.primaries], T[cs.transfer], M[cs.matrix], !!cs.fullRange] : null; };
    function output(r, chunk, meta) {
      const d = meta && meta.decoderConfig && meta.decoderConfig.description;
      if (d && !r.desc) { r.desc = new Uint8Array(d.buffer ? d.buffer.slice(d.byteOffset, d.byteOffset + d.byteLength) : d.slice(0)); r.color = color(meta.decoderConfig.colorSpace); }
      const b = new Uint8Array(chunk.byteLength); chunk.copyTo(b);
      const q = r.q.shift(); if (q !== undefined) { const d = performance.now() - q; r.encMs += d; r.encN++; if (d > r.encMax) r.encMax = d; }
      if (chunk.type === 'key') { group(r); r.keys.push(r.sizes.length + 1); }
      r.cur.push(b); r.curBytes += b.length; r.bytes += b.length; r.sizes.push(b.length); r.ts.push(Math.round(chunk.timestamp * TS / 1e6));
    }
    function open(m) {
      const r = { id: m.id, kind: m.kind, w: m.w, h: m.h, n: -1, keyN: -1e9, frames: 0, held: 0, bytes: 0, sizes: [], ts: [], keys: [], parts: [], cur: [], curBytes: 0, desc: null, color: null,
                  error: null, q: [], encMs: 0, encN: 0, encMax: 0, qMax: 0, pics: 0, src: null, dirty: false, picDirty: false, ov: { dim: false, badge: null, wait: null }, last: null, done: false, enc: null, canvas: null, g: null, base: null, bg: null };
      try {
        if (r.kind === 'screen') {
          r.canvas = new OffscreenCanvas(r.w, r.h); r.g = r.canvas.getContext('2d', { alpha: false });
          r.base = new OffscreenCanvas(r.w, r.h); r.bg = r.base.getContext('2d', { alpha: false });
        }
        r.enc = new VideoEncoder({ output: (c, md) => output(r, c, md), error: e => fail(r, e) }); r.enc.configure(m.config);
      } catch (e) { fail(r, e); }
      recs.set(r.id, r);
    }
    function step(r, n) {   // one frame of recorder r for slot n: a new picture when there is one, else the last frame again (constant frame rate)
      if (r.done || r.error || !r.src || n <= r.n) return;
      const qs = r.enc.encodeQueueSize; if (qs > r.qMax) r.qMax = qs;
      if (qs > 2) return;                                        // the encoder is behind: this slot is held (the frame before it lasts longer)
      const ts = r.n < 0 ? 0 : Math.round(n * 1e6 / fps), key = n - r.keyN >= 2 * fps;
      let f = null;
      try {
        if (r.dirty || !r.last) {
          if (r.kind === 'screen') {
            if (r.picDirty || !r.last) { recPicture(r.bg, r.src, !!r.ov.dim, r.w, r.h); r.picDirty = false; }
            const plain = !r.ov.badge && !r.ov.wait;
            if (!plain) { r.g.drawImage(r.base, 0, 0); recLayer(r.g, r.ov, font, r.w, r.h); }
            f = new VideoFrame(plain ? r.base : r.canvas, { timestamp: ts });
          } else f = new VideoFrame(r.src, { timestamp: ts });
          r.dirty = false; r.pics++;
        } else f = new VideoFrame(r.last, { timestamp: ts });
        r.q.push(performance.now()); r.enc.encode(f, { keyFrame: key });
      } catch (e) { fail(r, e); if (f) f.close(); return; }
      if (key) r.keyN = n;
      if (r.last) r.last.close(); r.last = f;
      if (r.n >= 0) r.held += n - r.n - 1; r.n = n; r.frames++;
    }
    async function finish(r) {
      if (r.done) return; r.done = true;
      try { if (r.enc && r.enc.state === 'configured') { await r.enc.flush(); r.enc.close(); } } catch (e) { fail(r, e); }
      if (r.last) { r.last.close(); r.last = null; }
      if (r.src && r.src.close) r.src.close(); r.src = null;
      const end = Math.max(r.n + 1, stopSlot, 1); let blob = null;
      if (!r.error) {
        if (!r.sizes.length) r.error = 'no picture';
        else if (!r.desc) r.error = 'no decoder configuration from the encoder';
        else {
          group(r); const endT = Math.round(end * TS / fps);
          r.durs = r.ts.map((t, i) => Math.max(1, (i + 1 < r.ts.length ? r.ts[i + 1] : endT) - t));
          try { blob = mp4File(r); } catch (e) { fail(r, e); }
        }
      }
      postMessage({ t: 'done', id: r.id, blob, secs: end / fps, frames: r.frames, held: r.held, bytes: r.bytes, w: r.w, h: r.h, error: r.error,
                    pics: r.pics, encAvg: r.encN ? r.encMs / r.encN : 0, encMax: r.encMax, qMax: r.qMax });
    }
    function finishAll() {
      if (ended) return; ended = true; clearInterval(timer);
      Promise.all(Array.from(recs.values()).map(finish)).then(() => postMessage({ t: 'end', slots: stopSlot, fps, t0 }));
    }
    function tick() {
      if (!started || ended) return;
      const t = now(), n = Math.floor((t - t0) * fps / 1000);
      if (stopSlot >= 0 && n >= stopSlot) { finishAll(); return; }
      if (maxSlots && n >= maxSlots && !maxed) { maxed = true; postMessage({ t: 'max' }); }
      let frames = 0, held = 0, bytes = 0, errs = 0;
      for (const r of recs.values()) { step(r, n); frames += r.frames; held += r.held; bytes += r.bytes; if (r.error) errs++; }
      if (t - statAt >= 500) { statAt = t; postMessage({ t: 'stat', n, frames, held, bytes, errs }); }
    }
    onmessage = e => {
      const m = e.data, r = m.id !== undefined ? recs.get(m.id) : null;
      if (m.t === 'open') open(m);
      else if (m.t === 'pic') { if (r && !r.done) { if (r.src && r.src.close) r.src.close(); r.src = m.bmp; r.dirty = true; r.picDirty = true; } else if (m.bmp && m.bmp.close) m.bmp.close(); }
      else if (m.t === 'ov') { if (r) { if (!!m.ov.dim !== !!r.ov.dim) r.picDirty = true; r.ov = m.ov; r.dirty = true; } }
      else if (m.t === 'font') { try { new FontFace(m.family, "url('" + m.url + "')", { weight: m.weight }).load().then(f => self.fonts.add(f), () => {}); } catch (err) { /* the system fonts draw the badge */ } }
      else if (m.t === 'start') { t0 = m.t0; fps = m.fps; font = m.font; maxSlots = m.maxSlots || 0; started = true; timer = setInterval(tick, Math.max(4, Math.floor(250 / fps))); tick(); }
      else if (m.t === 'stop') { stopSlot = Math.max(1, Math.floor((m.end - t0) * fps / 1000)); tick(); }
    };
  }
  function workerUrl() {
    const src = 'const TS = 90000;\n' + [mp4Str, mp4U32, mp4U16, mp4Box, mp4Table, mp4Moov, mp4File, roundRect, recPicture, recLayer, capWorker].map(String).join(';\n') + ';\ncapWorker();\n';
    return URL.createObjectURL(new Blob([src], { type: 'text/javascript' }));
  }

  // ------------------------------------------------------------------ painter: one DOM subtree -> 2D canvas, the way the page shows it
  // Computed styles are cached per element and read again while an element (or an ancestor) is in a transition: its class or style attribute
  // changed less than HOT ms ago. Boxes and text positions are read live at every paint, so widths that glide and numbers that count are followed.
  const HOT = 700;
  const P = { ver: 1, props: new WeakMap(), segs: new WeakMap(), asc: {}, mode: null, face: null, range: null, anyHot: false, reads: 0 };
  const clear = c => !c || c === 'transparent' || /[,/]\s*0\)$/.test(c);
  const px = v => parseFloat(v) || 0;
  function shadowOf(s) {   // the first outer shadow of a computed box-shadow / text-shadow value
    if (!s || s === 'none') return null;
    const m = /^(rgba?\([^)]*\)|color\([^)]*\)|#[0-9a-f]+|[a-z]+)\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px(?:\s+-?[\d.]+px)?(\s+inset)?/i.exec(s);
    return m && !m[5] && !clear(m[1]) ? { c: m[1], x: +m[2], y: +m[3], b: +m[4] } : null;
  }
  function radOf(v) { const a = v.split(' '), f = s => s.endsWith('%') ? { p: parseFloat(s) / 100, v: 0 } : { p: 0, v: px(s) }; return [f(a[0]), f(a[1] || a[0])]; }
  function readProps(e) {
    const cs = getComputedStyle(e), p = { none: cs.display === 'none' }; P.reads++;
    if (p.none) return p;
    p.vis = cs.visibility === 'visible'; p.op = parseFloat(cs.opacity); if (isNaN(p.op)) p.op = 1;
    p.bg = clear(cs.backgroundColor) ? null : cs.backgroundColor;
    const rr = [radOf(cs.borderTopLeftRadius), radOf(cs.borderTopRightRadius), radOf(cs.borderBottomRightRadius), radOf(cs.borderBottomLeftRadius)];
    p.rad = rr.some(c => c[0].p || c[0].v || c[1].p || c[1].v) ? rr : null;
    p.sh = shadowOf(cs.boxShadow);
    const bw = [px(cs.borderTopWidth), px(cs.borderRightWidth), px(cs.borderBottomWidth), px(cs.borderLeftWidth)];
    p.bs = [cs.borderTopStyle, cs.borderRightStyle, cs.borderBottomStyle, cs.borderLeftStyle];
    p.bc = [cs.borderTopColor, cs.borderRightColor, cs.borderBottomColor, cs.borderLeftColor];
    for (let i = 0; i < 4; i++) if (p.bs[i] === 'none' || p.bs[i] === 'hidden' || clear(p.bc[i])) bw[i] = 0;
    p.bw = bw.some(Boolean) ? bw : null;
    p.clip = cs.overflowX !== 'visible' || cs.overflowY !== 'visible';
    p.ell = p.clip && cs.textOverflow === 'ellipsis'; p.padR = px(cs.paddingRight);
    p.color = cs.color; p.tnum = /tnum/.test(cs.fontFeatureSettings) || /tabular/.test(cs.fontVariantNumeric);
    p.font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + cs.fontSize + ' ' + (p.tnum && P.face ? "'" + P.face + "', " : '') + cs.fontFamily;
    p.ls = cs.letterSpacing === 'normal' ? '0px' : cs.letterSpacing;
    p.ts = shadowOf(cs.textShadow); p.pre = cs.whiteSpace.indexOf('pre') === 0; p.up = cs.textTransform === 'uppercase';
    return p;
  }
  function propsOf(e, hot, now) {
    const sig = (e.getAttribute('class') || '') + '\u0001' + (e.getAttribute('style') || ''); let c = P.props.get(e);
    if (!c) { c = { sig, ver: -1, p: null, until: 0, hot: false }; P.props.set(e, c); }
    else if (c.sig !== sig) { c.sig = sig; c.until = now + HOT; }
    c.hot = hot || now < c.until;
    if (c.hot || c.ver !== P.ver) { c.p = readProps(e); c.ver = c.hot ? -1 : P.ver; }
    return c;
  }
  function boxPath(g, x, y, w, h, rad, ins) {   // the border box, or with ins = [top, right, bottom, left] the padding box inside it
    g.beginPath();
    let R = null;
    if (rad) {
      R = rad.map(c => [c[0].p ? c[0].p * w : c[0].v, c[1].p ? c[1].p * h : c[1].v]);
      const f = Math.min(1, w / (R[0][0] + R[1][0] || 1), w / (R[3][0] + R[2][0] || 1), h / (R[0][1] + R[3][1] || 1), h / (R[1][1] + R[2][1] || 1));
      if (f < 1) for (const c of R) { c[0] *= f; c[1] *= f; }
    }
    if (ins) {
      x += ins[3]; y += ins[0]; w -= ins[1] + ins[3]; h -= ins[0] + ins[2];
      if (R) { R[0][0] -= ins[3]; R[0][1] -= ins[0]; R[1][0] -= ins[1]; R[1][1] -= ins[0]; R[2][0] -= ins[1]; R[2][1] -= ins[2]; R[3][0] -= ins[3]; R[3][1] -= ins[2]; }
    }
    if (w <= 0 || h <= 0) return false;
    if (R && g.roundRect) g.roundRect(x, y, w, h, R.map(c => ({ x: Math.max(0, c[0]), y: Math.max(0, c[1]) }))); else g.rect(x, y, w, h);
    return true;
  }
  function edge(g, x0, y0, x1, y1, w, style, color) {
    const L = Math.abs(x1 - x0) + Math.abs(y1 - y0); if (L <= 0) return;
    g.strokeStyle = color; g.lineWidth = w; g.lineCap = 'butt';
    if (style === 'dashed' || style === 'dotted') {
      const d = style === 'dotted' ? w : (w >= 3 ? 2 * w : 3 * w), nd = Math.max(2, Math.round((L + d) / (2 * d))), gap = (L - nd * d) / (nd - 1);
      g.setLineDash(gap > 0 ? [d, gap] : []);
    }
    g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1); g.stroke(); g.setLineDash([]);
  }
  function drawBox(g, p, x, y, w, h, k) {
    if (p.bg && w > 0 && h > 0 && boxPath(g, x, y, w, h, p.rad)) {
      g.fillStyle = p.bg;
      if (p.sh) { g.shadowColor = p.sh.c; g.shadowOffsetX = p.sh.x * k; g.shadowOffsetY = p.sh.y * k; g.shadowBlur = p.sh.b * k; g.fill(); g.shadowColor = 'transparent'; g.shadowBlur = 0; g.shadowOffsetX = g.shadowOffsetY = 0; }
      else g.fill();
    }
    if (!p.bw) return;
    const b = p.bw, c = p.bc, s = p.bs;
    if (b[0] === b[1] && b[0] === b[2] && b[0] === b[3] && c[0] === c[1] && c[0] === c[2] && c[0] === c[3] && s[0] === 'solid' && s[1] === 'solid' && s[2] === 'solid' && s[3] === 'solid') {
      const t = b[0] / 2; if (boxPath(g, x, y, w, h, p.rad, [t, t, t, t])) { g.strokeStyle = c[0]; g.lineWidth = b[0]; g.stroke(); }
      return;
    }
    if (b[0]) edge(g, x, y + b[0] / 2, x + w, y + b[0] / 2, b[0], s[0], c[0]);
    if (b[2]) edge(g, x, y + h - b[2] / 2, x + w, y + h - b[2] / 2, b[2], s[2], c[2]);
    if (b[3]) edge(g, x + b[3] / 2, y, x + b[3] / 2, y + h, b[3], s[3], c[3]);
    if (b[1]) edge(g, x + w - b[1] / 2, y, x + w - b[1] / 2, y + h, b[1], s[1], c[1]);
  }
  function drawCanvas(g, cv, r) {   // a chart: its own pixels, on whole device pixels
    if (!cv.width || !cv.height || r.width <= 0 || r.height <= 0) return;
    const t = g.getTransform(), x = Math.round(r.left * t.a + t.e), y = Math.round(r.top * t.d + t.f), w = Math.round(r.width * t.a), h = Math.round(r.height * t.d);
    g.save(); g.setTransform(1, 0, 0, 1, 0, 0);
    try { g.drawImage(cv, x, y, Math.abs(w - cv.width) <= 2 ? cv.width : w, Math.abs(h - cv.height) <= 2 ? cv.height : h); } catch (e) { /* a canvas with no backing store yet */ }
    g.restore();
  }
  const isDigit = ch => ch >= '0' && ch <= '9', isSpace = ch => ch === ' ' || ch === '\n' || ch === '\t' || ch === '\r';
  function segsOf(node, data, a, b, key) {   // a text that wraps: [start, end) of each line, found once per text and width
    const c = P.segs.get(node); if (c && c.key === key) return c.v;
    const R = P.range, v = []; let top = null, s = a, last = a;
    for (let i = a; i < b; i++) {
      if (isSpace(data[i])) continue;
      R.setStart(node, i); R.setEnd(node, i + 1); const rs = R.getClientRects(); if (!rs.length) continue;
      const t = rs[0].top;
      if (top === null) { top = t; s = i; } else if (Math.abs(t - top) > 2) { v.push([s, last + 1]); top = t; s = i; }
      last = i;
    }
    if (top !== null) v.push([s, last + 1]);
    P.segs.set(node, { key, v }); return v;
  }
  function drawText(g, node, p, k, ell, pw) {
    const data = node.data; let a = 0, b = data.length;
    if (!p.pre) { while (a < b && isSpace(data[a])) a++; while (b > a && isSpace(data[b - 1])) b--; }
    if (a >= b) return;
    const R = P.range; R.setStart(node, a); R.setEnd(node, b);
    const rects = R.getClientRects(); if (!rects.length) return;
    g.font = p.font; g.letterSpacing = p.ls; g.fillStyle = p.color; g.textBaseline = 'alphabetic';
    let asc = P.asc[p.font]; if (asc === undefined) asc = P.asc[p.font] = Math.round(g.measureText('Hg').fontBoundingBoxAscent);
    if (p.ts) { g.shadowColor = p.ts.c; g.shadowOffsetX = p.ts.x * k; g.shadowOffsetY = p.ts.y * k; g.shadowBlur = p.ts.b * k; }
    const cells = P.mode === 'cells' && p.tnum && !ell;
    const line = (s, e, rr) => {
      const y = rr.top + asc;
      if (cells) {                                    // the page shows digits in cells of one width: each digit in the middle of its own cell
        let i = s, any = false;
        for (let j = s; j < e; j++) if (isDigit(data[j])) { any = true; break; }
        if (any) {
          while (i < e) {
            let j = i + 1; const dg = isDigit(data[i]);
            if (!dg) while (j < e && !isDigit(data[j])) j++;
            R.setStart(node, i); R.setEnd(node, i + 1); const cr = R.getBoundingClientRect();
            let str = data.slice(i, j); if (!p.pre) str = str.replace(/\s+/g, ' '); if (p.up) str = str.toUpperCase();
            g.fillText(str, dg ? cr.left + (cr.width - g.measureText(str).width) / 2 : cr.left, y); i = j;
          }
          return;
        }
      }
      let str = data.slice(s, e); if (!p.pre) str = str.replace(/\s+/g, ' '); if (p.up) str = str.toUpperCase();
      if (ell) {
        if (ell.done) return;
        if (rr.left + g.measureText(str).width > ell.right + 0.5) {
          const lim = ell.right - g.measureText('…').width; let n = str.length;
          while (n > 0 && rr.left + g.measureText(str.slice(0, n)).width > lim) n--;
          g.fillText(str.slice(0, n) + '…', rr.left, y); ell.done = true; return;
        }
      }
      g.fillText(str, rr.left, y);
    };
    if (rects.length === 1) line(a, b, rects[0]);
    else for (const sg of segsOf(node, data, a, b, a + ':' + b + ':' + pw + ':' + rects.length + ':' + data)) { R.setStart(node, sg[0]); R.setEnd(node, sg[1]); line(sg[0], sg[1], R.getBoundingClientRect()); }
    if (p.ts) { g.shadowColor = 'transparent'; g.shadowBlur = 0; g.shadowOffsetX = g.shadowOffsetY = 0; }
  }
  function walk(g, e, alpha, hot, now, k, ell) {
    const c = propsOf(e, hot, now), p = c.p; if (p.none) return;
    hot = c.hot; if (hot) P.anyHot = true;
    const a = alpha * p.op; if (a < 0.01) return;
    const r = e.getBoundingClientRect(), sn = v => Math.round(v * k) / k, x = sn(r.left), y = sn(r.top), w = sn(r.right) - x, h = sn(r.bottom) - y;
    if (p.vis) {
      g.globalAlpha = a; drawBox(g, p, x, y, w, h, k);
      if (e.tagName === 'CANVAS') { drawCanvas(g, e, r); return; }
    }
    if (!e.firstChild) return;
    let clipped = false;
    if (p.clip) { g.save(); clipped = true; if (boxPath(g, x, y, w, h, p.rad, p.bw || [0, 0, 0, 0])) g.clip(); else { g.restore(); return; } }
    if (p.ell && e.scrollWidth > e.clientWidth) ell = { right: r.right - (p.bw ? p.bw[1] : 0) - p.padR, done: false };
    for (let n = e.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3) { if (p.vis && !(ell && ell.done)) { g.globalAlpha = a; drawText(g, n, p, k, ell, Math.round(r.width)); } }
      else if (n.nodeType === 1) walk(g, n, a, hot, now, k, ell);
    }
    if (clipped) g.restore();
  }
  function paintUnit(u, now) {   // -> false: the unit is not on the page (its last picture stays)
    const e = u.el, g = u.g, k = u.k; if (!e || !e.isConnected) return false;
    const r0 = e.getBoundingClientRect(); if (r0.width < 1 || r0.height < 1) return false;
    g.setTransform(1, 0, 0, 1, 0, 0); g.globalAlpha = 1; g.fillStyle = D.cv('--bg') || '#f2f4f6'; g.fillRect(0, 0, u.pw, u.ph);
    g.setTransform(k, 0, 0, k, -Math.round(r0.left * k), -Math.round(r0.top * k));
    P.anyHot = false; walk(g, e, 1, false, now, k, null); u.hot = P.anyHot;
    u.cw = Math.round(r0.width); u.ch = Math.round(r0.height);
    return true;
  }
  async function fontMode() {   // how the canvas gets the page's equal-width digits (canvas text takes no font-feature-settings)
    const fam = "'Pretendard Variable'";
    try { await document.fonts.load('600 16px ' + fam); await document.fonts.load('400 16px ' + fam); } catch (e) { /* the page's fallback fonts are used */ }
    if (P.mode) return;
    const g = new OffscreenCanvas(8, 8).getContext('2d'), ones = '1111111111', wOf = f => { g.font = '500 40px ' + f; return g.measureText(ones).width; };
    const sp = el('span', { style: { position: 'fixed', left: '-9999px', top: '0', font: '500 40px ' + fam, fontFeatureSettings: "'tnum' 1, 'ss01' 1", letterSpacing: '0px', whiteSpace: 'nowrap' } }, ones);
    document.body.appendChild(sp); const dom = sp.getBoundingClientRect().width; sp.remove();
    P.mode = 'cells';
    if (Math.abs(wOf(fam) - dom) < 0.5) P.mode = 'plain';
    else {
      try {
        const f = new FontFace('Pretendard Cap', "url('/static/fonts/PretendardVariable.woff2')", { weight: '45 920', featureSettings: "'tnum' 1, 'ss01' 1" });
        await f.load(); document.fonts.add(f);
        if (Math.abs(wOf("'Pretendard Cap'") - dom) < 0.5) { P.mode = 'face'; P.face = 'Pretendard Cap'; }
      } catch (e) { /* per-digit placement */ }
    }
    P.ver++; P.asc = {};
  }

  // ------------------------------------------------------------------ units (graphs / figures of the measurement tabs) and viewer screens
  const CAPTABS = ['ov', 'va', 'nv', 'gpu', 'radio', 'demo'], LS = 'dash.cap.sel';
  const LEVELS = [[8192, 245760, '28'], [8704, 522240, '2a'], [22080, 589824, '32'], [36864, 983040, '33'], [36864, 2073600, '34']];   // H.264: frame size (MBs), MB/s, level
  const K = { state: 'idle', tabs: [], capw: 0, units: [], screens: [], worker: null, url: null, t0: 0, end: 0, fps: 30, budget: 14, raf: 0, wd: 0, ui: 0, kill: 0, lastFrame: 0,
              stat: null, done: {}, files: [], info: null, hidAt: 0, hiddenMs: 0, paintMs: 0, paintN: 0, paintMax: 0, frames: 0, rp0: null, prefix: 'dash', stamp: '' };
  const recNow = () => performance.timeOrigin + performance.now();
  const capConf = () => (S.meta && S.meta.cap) || {};
  const tabLabel = t => { const b = $('#tabs .tabbtn[data-tab="' + t + '"]'); return b ? b.textContent.trim() : t; };
  const even = n => Math.max(16, (Math.round(n) + 1) & ~1);
  const slug = s => s.replace(/[^0-9A-Za-z가-힣]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'item';
  const p2 = n => (n < 10 ? '0' : '') + n;
  const wall = t => { const d = new Date(t); return p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds()) + '.' + String(d.getMilliseconds()).padStart(3, '0'); };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const settle = () => Promise.race([new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 90)))), sleep(700)]);   // layout + chart resize

  function scan(t) {   // the units of one tab: every card and every top-level tile, in page order
    const host = $('#tab-' + t), out = []; if (!host) return out;
    for (const e of host.querySelectorAll('.card, .tile')) {
      if (e.parentElement.closest('.card')) continue;
      const tt = e.querySelector('.cardh .t') || e.querySelector('.ueh .nm') || e.querySelector('.lb span') || e.querySelector('.lb');
      out.push({ tab: t, i: out.length, title: ((tt ? tt.textContent : '') || '').trim() || '항목', el: e, tile: e.classList.contains('tile') });
    }
    return out;
  }
  function setTabs(tabs) {   // these tabs are laid out off screen (at the page's width) so that their units exist, update and can be painted
    const main = $('main');
    if (tabs.length && !K.capw) { const cs = getComputedStyle(main); K.capw = Math.max(320, Math.round(main.clientWidth - px(cs.paddingLeft) - px(cs.paddingRight))); }
    if (!tabs.length) K.capw = 0;
    for (const t of CAPTABS) { const s = $('#tab-' + t), on = tabs.indexOf(t) >= 0; s.classList.toggle('capon', on); if (on) s.style.setProperty('--capw', K.capw + 'px'); else s.style.removeProperty('--capw'); }
    K.tabs = tabs;
  }
  function watch(u) {
    if (u.mo) u.mo.disconnect(); u.mo = null; u.cvs = []; u.cvv = []; u.rescan = false; u.dirty = true;
    if (!u.el) return;
    u.cvs = Array.from(u.el.querySelectorAll('canvas'));
    u.mo = new MutationObserver(ms => { u.dirty = true; for (const m of ms) if (m.type === 'childList') { u.rescan = true; break; } });
    u.mo.observe(u.el, { subtree: true, childList: true, attributes: true, characterData: true });
  }
  function changed(u, now) {
    if (u.dirty || u.hot) return true;
    for (let i = 0; i < u.cvs.length; i++) if (u.cvs[i]._v !== u.cvv[i]) return true;
    return now - u.paintedAt > 1000;                          // a move or a resize that no change inside the unit announces
  }
  function paint(u, now) {
    u.dirty = false; u.paintedAt = now;
    if (u.rescan && u.el) { u.cvs = Array.from(u.el.querySelectorAll('canvas')); u.rescan = false; }
    u.cvv = u.cvs.map(c => c._v);
    if (!paintUnit(u, now)) { u.hot = false; return; }
    const bmp = u.cv.transferToImageBitmap(); K.worker.postMessage({ t: 'pic', id: u.id, bmp }, [bmp]); u.paints++;
    const d = performance.now() - now; K.paintMs += d; K.paintN++; if (d > K.paintMax) K.paintMax = d;
  }
  function overlayOf(p) {   // what the screen shows over its picture, as the /viewer recorder reads it
    const ov = { dim: false, badge: null, wait: null }; if (!D.viewer.conf().REC_BADGE) return ov;
    ov.dim = p.stage.classList.contains('frozen') && p.stage.classList.contains('dim');
    if (p.badge.classList.contains('show')) ov.badge = { grey: p.badge.classList.contains('grey'), a: p.badgeMain.textContent, b: p.badgeSub.textContent };
    if (p.wait.classList.contains('show')) ov.wait = p.wait.textContent;
    return ov;
  }
  function screenOpen(p, id, n) {
    const s = { id, p, n, name: p.name, kind: p.kind, ue: p.ue, w: 1920, h: 1080, snap: false, again: false, ovKey: '', pics: 0 };
    s.shot = () => {
      if (s.snap) { s.again = true; return; } s.snap = true;
      createImageBitmap(p.canvas).then(bmp => { if (K.worker && p.rec === s) { K.worker.postMessage({ t: 'pic', id, bmp }, [bmp]); s.pics++; } else bmp.close(); }, () => {})
        .then(() => { s.snap = false; if (s.again) { s.again = false; s.shot(); } });
    };
    p.rec = s; p.onPic = s.shot; return s;
  }
  function overlay(s) { const ov = overlayOf(s.p), key = JSON.stringify(ov); if (key !== s.ovKey) { s.ovKey = key; K.worker.postMessage({ t: 'ov', id: s.id, ov }); } }
  async function probe(w, h, bps, soft) {
    const wm = Math.ceil(w / 16), hm = Math.ceil(h / 16), fs = Math.max(wm * hm, Math.ceil(Math.max(wm, hm) * Math.max(wm, hm) / 8)), lv = LEVELS.find(l => fs <= l[0] && wm * hm * K.fps <= l[1]);
    if (!lv) return null;
    for (const prof of ['6400', '4d00', '42e0']) {
      const cfg = { codec: 'avc1.' + prof + lv[2], width: w, height: h, bitrate: Math.round(bps), framerate: K.fps, latencyMode: 'quality', avc: { format: 'avc' } };
      if (soft) cfg.hardwareAcceleration = 'prefer-software';
      try { const r = await VideoEncoder.isConfigSupported(cfg); if (r && r.supported) return cfg; } catch (e) { /* next profile */ }
    }
    return null;
  }
  function support() {
    if (!window.isSecureContext) {
      const port = S.meta && S.meta.tls_port;
      return port ? { ok: false, url: 'https://' + location.hostname + ':' + port + location.pathname, why: 'Capture는 브라우저의 동영상 인코더(WebCodecs)를 쓰고, 이 기능은 https 주소에서만 열립니다. 아래 주소로 이 대시보드를 열어 주세요 (인증서 경고는 뷰어와 같은 방법으로 넘어갑니다).' }
                  : { ok: false, why: 'Capture에는 https 주소가 필요한데 대시보드의 HTTPS 포트가 꺼져 있습니다 (.env DASH_TLS_PORT).' };
    }
    if (!window.VideoEncoder || !window.VideoFrame || !window.OffscreenCanvas || !window.Worker) return { ok: false, why: '이 브라우저에는 WebCodecs 동영상 인코더가 없습니다. 최신 Chrome 또는 Edge에서 열어 주세요.' };
    return { ok: true };
  }

  // ------------------------------------------------------------------ recording
  function frame() {
    if (K.state !== 'rec') return;
    const now = performance.now(); if (now - K.lastFrame < 1000 / K.fps - 3) return;
    K.lastFrame = now; K.frames++;
    for (const s of K.screens) overlay(s);
    const due = K.units.filter(u => !u.error && changed(u, now)); if (!due.length) return;
    due.sort((a, b) => a.paintedAt - b.paintedAt);            // the unit that waited longest first; whatever does not fit the budget comes next frame
    const until = now + K.budget;
    for (const u of due) { paint(u, performance.now()); if (performance.now() > until) break; }
  }
  function loop() { if (K.state !== 'rec') return; K.raf = requestAnimationFrame(loop); frame(); }
  function release() {
    cancelAnimationFrame(K.raf); clearInterval(K.wd); clearInterval(K.ui); clearTimeout(K.kill);
    if (K.worker) { K.worker.terminate(); K.worker = null; }
    if (K.url) { URL.revokeObjectURL(K.url); K.url = null; }
    for (const u of K.units) { if (u.mo) u.mo.disconnect(); u.mo = null; u.cv = null; u.g = null; }
    for (const s of K.screens) { if (s.p.rec === s) { s.p.rec = null; s.p.onPic = null; } }
    if (D.viewer) { D.viewer.hold('cap', false); D.viewer.hold('pick', false); }
    setTabs([]);
  }
  function fail(msg) { release(); K.units = []; K.screens = []; K.state = 'idle'; head(); D.toast(msg, 6000); if (D.app) D.app.render(); }
  async function start(sel) {
    if (K.state !== 'pick') return;
    K.state = 'arm'; head();
    const cap = capConf(), rp = S.rp;
    K.fps = Math.max(5, Math.min(60, Math.round(cap.fps || 30))); K.budget = Math.max(6, 450 / K.fps);
    for (const f of K.files) if (f.url) URL.revokeObjectURL(f.url);
    K.files = []; K.done = {}; K.stat = null; K.hiddenMs = 0; K.hidAt = document.hidden ? performance.now() : 0; K.paintMs = 0; K.paintN = 0; K.paintMax = 0; K.frames = 0; K.info = null;
    try {
      await fontMode();
      K.units = sel.units; K.screens = [];
      setTabs(CAPTABS.filter(t => K.units.some(u => u.tab === t))); if (D.app) D.app.render(); await settle();
      const k0 = cap.scale > 0 ? cap.scale : (window.devicePixelRatio || 1), mbps = cap.mbps || 6;
      let id = 0;
      for (const u of K.units) {
        u.id = 'u' + (id++); u.paints = 0; u.paintedAt = 0; u.hot = false; u.error = null;
        const r = u.el.getBoundingClientRect(); let k = Math.min(k0, 3840 / r.width, 2160 / r.height), cfg = null;
        for (let i = 0; i < 5 && !cfg; i++) {
          u.pw = even(r.width * k); u.ph = even(r.height * k);
          const bps = Math.max(8e5, mbps * 1.5e6 * u.pw * u.ph / (1920 * 1080));
          cfg = await probe(u.pw, u.ph, bps, true) || await probe(u.pw, u.ph, bps, false);
          if (!cfg) k *= 0.75;
        }
        if (!cfg) { u.error = '이 크기(' + u.pw + '×' + u.ph + ')는 이 브라우저가 H.264로 인코딩하지 못합니다'; continue; }
        u.k = k; u.cfg = cfg; u.cv = new OffscreenCanvas(u.pw, u.ph); u.g = u.cv.getContext('2d', { alpha: false }); watch(u);
      }
      let scfg = null;
      if (sel.panes.length) { scfg = await probe(1920, 1080, mbps * 1e6, false); if (!scfg) throw new Error('이 브라우저는 1920×1080 H.264 인코딩을 지원하지 않습니다'); D.viewer.hold('cap', true); }
      if (D.viewer) D.viewer.hold('pick', false);
      if (K.state !== 'arm') return;
      K.url = workerUrl(); K.worker = new Worker(K.url);
      K.worker.onmessage = onWorker; K.worker.onerror = e => { if (K.state === 'rec' || K.state === 'arm') fail('Capture 작업자 오류: ' + (e.message || '알 수 없음')); else if (K.state === 'save') finishUp('Capture 작업자 오류: ' + (e.message || '알 수 없음')); };
      K.worker.postMessage({ t: 'font', family: 'Pretendard Variable', url: location.origin + '/static/fonts/PretendardVariable.woff2', weight: '45 920' });
      for (const u of K.units) if (!u.error) K.worker.postMessage({ t: 'open', id: u.id, kind: 'unit', w: u.pw, h: u.ph, config: u.cfg });
      sel.panes.forEach((p, i) => { const s = screenOpen(p, 's' + i, i); K.screens.push(s); K.worker.postMessage({ t: 'open', id: s.id, kind: 'screen', w: s.w, h: s.h, config: scfg }); });
      const now = performance.now();
      for (const u of K.units) if (!u.error) paint(u, now);                         // every file starts with a picture at the common start
      for (const s of K.screens) { overlay(s); const bmp = await createImageBitmap(s.p.canvas); K.worker.postMessage({ t: 'pic', id: s.id, bmp }, [bmp]); }
      if (K.state !== 'arm') return;
      K.rp0 = rp ? { name: rp.name || rp.id || '', pos: rp.pos } : null; K.prefix = rp ? 'replay' : slug(String(cap.prefix || 'dash'));
      K.t0 = recNow(); const d = new Date(K.t0);
      K.stamp = d.getFullYear() + p2(d.getMonth() + 1) + p2(d.getDate()) + '_' + p2(d.getHours()) + p2(d.getMinutes()) + p2(d.getSeconds());
      K.worker.postMessage({ t: 'start', t0: K.t0, fps: K.fps, font: getComputedStyle(document.body).fontFamily, maxSlots: Math.round((cap.max_min || 20) * 60 * K.fps) });
      K.state = 'rec'; K.lastFrame = 0; loop();
      K.wd = setInterval(() => { if (performance.now() - K.lastFrame > 200) frame(); }, 250);   // a hidden tab gets no animation frames
      K.ui = setInterval(head, 500); head();
    } catch (e) { fail('Capture를 시작하지 못했습니다: ' + (e.message || e)); }
  }
  function stop(why) {
    if (K.state !== 'rec') return;
    cancelAnimationFrame(K.raf); clearInterval(K.wd);
    const now = performance.now(); for (const u of K.units) if (!u.error && changed(u, now)) paint(u, performance.now());
    for (const s of K.screens) overlay(s);
    if (K.hidAt) { K.hiddenMs += performance.now() - K.hidAt; K.hidAt = 0; }
    K.state = 'save'; K.why = why || ''; K.rp1 = S.rp ? { name: S.rp.name || S.rp.id || '', pos: S.rp.pos } : null;
    K.end = recNow() + 120; K.worker.postMessage({ t: 'stop', end: K.end });
    K.kill = setTimeout(() => finishUp('인코더가 60초 안에 끝나지 않았습니다'), 60000); head();
  }
  function onWorker(e) {
    const m = e.data;
    if (m.t === 'stat') K.stat = m;
    else if (m.t === 'max') { if (K.state === 'rec') { D.toast('Capture 최대 길이(' + (capConf().max_min || 20) + '분)에 닿아 종료합니다', 6000); stop('max'); } }
    else if (m.t === 'done') K.done[m.id] = m;
    else if (m.t === 'end') { K.endInfo = m; finishUp(''); }
  }
  function finishUp(err) {
    if (K.state !== 'save') return;
    const files = [], secs = K.endInfo ? K.endInfo.slots / K.endInfo.fps : (K.end - K.t0) / 1000; let nn = 0;
    const add = (x, name, title) => {
      const d = K.done[x.id] || {}, f = { name, title, w: d.w || x.pw || x.w, h: d.h || x.ph || x.h, secs: d.secs || 0, frames: d.frames || 0, held: d.held || 0, bytes: d.blob ? d.blob.size : 0,
                                          url: d.blob ? URL.createObjectURL(d.blob) : null, error: x.error || d.error || (d.blob ? null : (err || '파일이 만들어지지 않았습니다')), scale: x.k || null, lost: !!x.lost,
                                          pics: d.pics || 0, encAvg: d.encAvg || 0, encMax: d.encMax || 0, qMax: d.qMax || 0, paints: x.paints || 0, codec: (x.cfg && x.cfg.codec) || '', soft: !!(x.cfg && x.cfg.hardwareAcceleration) };
      files.push(f);
    };
    for (const u of K.units) add(u, K.prefix + '_cap_' + K.stamp + '_' + p2(++nn) + '_' + u.tab + '_' + slug(u.title) + '.mp4', tabLabel(u.tab) + ' · ' + u.title);
    for (const s of K.screens) add(s, K.prefix + '_cap_' + K.stamp + '_view_' + (s.kind === 'va' ? 'va' : 'nonva') + '_' + slug(String(s.ue)) + '.mp4', '뷰어 · ' + s.name);
    K.info = { t0: K.t0, end: K.t0 + secs * 1000, secs, fps: K.fps, err: err || '', why: K.why, hiddenMs: Math.round(K.hiddenMs), mode: P.mode, rp0: K.rp0, rp1: K.rp1,
               paintAvg: K.paintN ? K.paintMs / K.paintN : 0, paintMax: K.paintMax, paints: K.paintN, frames: K.frames, units: K.units.length, screens: K.screens.length };
    release(); K.files = files; K.units = []; K.screens = []; K.endInfo = null; K.state = 'idle'; head(); if (D.app) D.app.render();
    saveAll(); result();
  }
  function save(f) { if (!f.url) return; const a = el('a', { href: f.url, download: f.name, style: { display: 'none' } }); document.body.appendChild(a); a.click(); setTimeout(() => a.remove(), 1000); }
  function saveAll() { K.files.filter(f => f.url).forEach((f, i) => setTimeout(() => save(f), 400 * i)); }

  // ------------------------------------------------------------------ header, checklist, result
  function head() {
    const b = $('#capBtn'), ind = $('#capInd'); if (!b || !ind) return;
    const rec = K.state === 'rec';
    b.textContent = rec ? 'Capture 종료' : K.state === 'save' ? '저장 중…' : K.state === 'arm' ? '준비 중…' : 'Capture';
    b.classList.toggle('danger', rec); b.disabled = K.state === 'arm' || K.state === 'save'; ind.hidden = !rec;
    if (rec) $('#capTime').textContent = 'Capture ' + D.mmss((recNow() - K.t0) / 1000) + ' · ' + (K.units.filter(u => !u.error).length + K.screens.length) + '개' + (K.stat ? ' · ' + D.bytes(K.stat.bytes) : '');
  }
  function cancelPick() { if (K.state !== 'pick') return; K.state = 'idle'; K.closePick = null; setTabs([]); if (D.viewer) D.viewer.hold('pick', false); head(); if (D.app) D.app.render(); }
  async function pick() {
    if (K.state !== 'idle') return;
    if (!(S.rp ? S.rp.meta : S.meta)) { D.toast('아직 표시할 데이터가 없습니다'); return; }
    K.state = 'pick'; setTabs(CAPTABS.slice()); D.app.render();
    const live = !S.rp && D.viewer && S.meta && S.meta.viewer && S.meta.viewer.relay, panes = live ? D.viewer.panes() : [];
    if (panes.length) D.viewer.hold('pick', true);            // the screens are already running when the capture starts
    await settle(); if (K.state !== 'pick') return;
    const sup = support(), cap = capConf(), k0 = cap.scale > 0 ? cap.scale : (window.devicePixelRatio || 1), keyOf = u => u.tab + '/' + u.i + '/' + u.title;
    let saved = null; try { saved = JSON.parse(localStorage.getItem(LS) || 'null'); } catch (e) { /* no stored choice */ }
    const was = new Set((saved && saved.units) || []), wasP = new Set((saved && saved.panes) || []);
    const groups = CAPTABS.map(t => ({ label: tabLabel(t), units: scan(t) })).filter(g => g.units.length), boxes = [];
    D.app.modal(true, (m, close) => {
      K.closePick = close;
      const err = el('div.err'), count = el('span.capcount'), go = el('button.btn.primary', { disabled: !sup.ok || null }, 'Capture 시작');
      const refresh = () => { const n = boxes.filter(b => b.inp.checked).length; count.textContent = '선택 ' + n + '개 = MP4 ' + n + '개'; for (const g of m.querySelectorAll('.capgrp')) { const a = g.querySelectorAll('.capitem input:not(:disabled)'), all = g.querySelector('.capall input'); if (all) { const c = Array.from(a).filter(x => x.checked).length; all.checked = c > 0 && c === a.length; all.indeterminate = c > 0 && c < a.length; } } };
      const item = (title, size, checked, ref, off) => {
        const inp = el('input', { type: 'checkbox', disabled: off || null }); inp.checked = !!checked && !off; inp.addEventListener('change', refresh);
        if (!off) boxes.push(Object.assign({ inp }, ref));
        return el('label.capitem' + (off ? '.dis' : ''), inp, el('span.nm', title), el('span.sz', size));
      };
      const group = (label, items, note, off) => {
        const all = el('input', { type: 'checkbox', disabled: off || null });
        all.addEventListener('change', () => { for (const x of g.querySelectorAll('.capitem input:not(:disabled)')) x.checked = all.checked; refresh(); });
        const g = el('div.capgrp', el('div.capgh', el('label.capall', all, el('b', label)), el('span.s', items.length + '개')), el('div.capitems', items), note ? el('div.note', note) : null);
        return g;
      };
      m.appendChild(el('h2', 'Capture'));
      m.appendChild(el('div.desc', '고른 항목마다 MP4 파일 하나를 이 PC로 내려받습니다. 모든 파일은 같은 순간에 시작해 같은 순간에 끝나므로, 나란히 놓으면 시간이 맞습니다.'
        + (S.rp ? ' 지금은 Replay 중이므로 Replay 화면에 보이는 그대로(배속 · 일시정지 · 이동 포함) 기록됩니다.' : '')));
      if (!sup.ok) m.appendChild(el('div.capwarn', sup.why, sup.url ? el('div', el('a', { href: sup.url }, sup.url)) : null));
      const body = el('div.capbody');
      for (const g of groups) body.appendChild(group(g.label, g.units.map(u => { const r = u.el.getBoundingClientRect(); return item(u.title, even(r.width * k0) + '×' + even(r.height * k0), was.has(keyOf(u)), { unit: u }); })));
      const names = panes.length ? panes : [{ name: 'VA UE 1' }, { name: 'VA UE 2' }, { name: 'Non-VA UE 1' }, { name: 'Non-VA UE 2' }];
      body.appendChild(group('뷰어 화면', names.map(p => item(p.name, '1920×1080', p.key && wasP.has(p.key), { pane: p }, !panes.length)),
        panes.length ? '뷰어 탭의 네 화면입니다. 화면마다 MP4 하나이며, QoS를 놓쳐 멈춘 화면과 배지도 그대로 기록됩니다.'
                     : (S.rp ? '뷰어 화면은 실시간 영상이라 Replay에서는 고를 수 없습니다. 저장된 측정에는 영상이 들어 있지 않습니다.' : '뷰어 중계가 꺼져 있어 고를 수 없습니다.'), !panes.length));
      m.appendChild(body);
      m.appendChild(el('div.note', K_NOTE(cap, k0)));
      m.appendChild(err);
      m.appendChild(el('div.acts', count, el('button.btn.ghost', { onclick: close }, '취소'), go));
      go.addEventListener('click', () => {
        const units = boxes.filter(b => b.unit && b.inp.checked).map(b => b.unit), ps = boxes.filter(b => b.pane && b.inp.checked).map(b => b.pane);
        if (!units.length && !ps.length) { err.textContent = '하나 이상 고르세요.'; return; }
        try { localStorage.setItem(LS, JSON.stringify({ units: units.map(keyOf), panes: ps.map(p => p.key) })); } catch (e) { /* not stored */ }
        K.closePick = null; start({ units, panes: ps }); close();
      });
      refresh();
      const mo = new MutationObserver(() => { if (!m.isConnected) { mo.disconnect(); cancelPick(); } }); mo.observe($('#modalHost'), { childList: true });
    });
  }
  function K_NOTE(cap, k0) {
    return (cap.fps || 30) + ' fps · 화면에 보이는 크기 × ' + (Math.round(k0 * 100) / 100) + ' · 최대 ' + (cap.max_min || 20) + '분. 녹화 중에는 이 대시보드 탭을 앞에 두세요(가려지면 그래프 갱신이 느려집니다). '
      + '파일은 "Capture 종료"를 누를 때 만들어지며, 그때까지 이 탭의 메모리에 있습니다. Chrome이 "여러 파일 다운로드"를 물으면 허용을 누르세요.';
  }
  function result() {
    const I = K.info; if (!I) return;
    const ok = K.files.filter(f => f.url), bad = K.files.filter(f => !f.url);
    D.app.modal(true, (m, close) => {
      m.appendChild(el('h2', 'Capture 결과'));
      m.appendChild(el('div.desc', 'MP4 ' + ok.length + '개 · 길이 ' + I.secs.toFixed(2) + '초 · ' + I.fps + ' fps · 시작 ' + wall(I.t0) + ' · 종료 ' + wall(I.end)
        + (I.rp0 ? ' · Replay "' + I.rp0.name + '" ' + D.mmss(I.rp0.pos) + ' → ' + D.mmss(I.rp1 ? I.rp1.pos : I.rp0.pos) : '')));
      if (I.err) m.appendChild(el('div.capwarn', I.err));
      const tb = el('tbody');
      for (const f of K.files) tb.appendChild(el('tr', el('td.l', f.title), el('td.l', f.url ? el('a', { href: f.url, download: f.name }, f.name) : f.name), el('td', f.w + '×' + f.h),
        el('td', f.url ? f.secs.toFixed(2) + ' s' : '–'), el('td', f.url ? String(f.frames) : '–'), el('td', f.url ? D.bytes(f.bytes) : '–'), el('td.l', f.error ? f.error : (f.lost ? '녹화 중 화면에서 사라진 구간이 있습니다' : ''))));
      m.appendChild(el('div.capbody', el('table.tbl.capfiles', el('thead', el('tr', el('th', '항목'), el('th', '파일 (누르면 다시 내려받기)'), el('th', '해상도'), el('th', '길이'), el('th', '프레임'), el('th', '크기'), el('th', '비고'))), tb)));
      const notes = ['파일은 이 탭의 메모리에 있습니다. 탭을 닫거나 새로 고치기 전에 내려받으세요.'];
      if (bad.length) notes.push(bad.length + '개 항목은 파일이 만들어지지 않았습니다(비고 참고).');
      if (I.hiddenMs > 500) notes.push('녹화 중 이 탭이 ' + (I.hiddenMs / 1000).toFixed(1) + '초 동안 가려져 있었습니다. 그 구간의 그래프는 갱신이 느립니다.');
      m.appendChild(el('div.note', notes.join(' ')));
      m.appendChild(el('div.acts', el('button.btn.ghost', { onclick: saveAll }, '모두 다시 내려받기'), el('button.btn.primary', { onclick: close }, '닫기')));
    });
  }
  function rebuilt() {   // the tabs were built again (live <-> Replay, new configuration): find the units again
    if (K.state === 'pick') { if (K.closePick) K.closePick(); else cancelPick(); return; }
    if (K.state !== 'rec' && K.state !== 'arm') return;
    const lists = {}; P.ver++;
    for (const u of K.units) {
      const list = lists[u.tab] || (lists[u.tab] = scan(u.tab)), x = (list[u.i] && list[u.i].title === u.title) ? list[u.i] : list.find(y => y.title === u.title);
      u.el = x ? x.el : null; if (!x) u.lost = true; if (u.cv) watch(u);
    }
  }
  function touch(flush) { if (flush) { P.ver++; P.asc = {}; } for (const u of K.units) u.dirty = true; }
  document.addEventListener('visibilitychange', () => {
    if (K.state !== 'rec') return;
    if (document.hidden) K.hidAt = performance.now(); else if (K.hidAt) { K.hiddenMs += performance.now() - K.hidAt; K.hidAt = 0; }
  });
  window.addEventListener('beforeunload', e => { if (K.state === 'rec' || K.state === 'save') { e.preventDefault(); e.returnValue = ''; } });
  P.range = document.createRange();
  D.cap = { click: () => { if (K.state === 'idle') pick(); else if (K.state === 'rec') stop(''); }, tabs: () => K.tabs, touch, rebuilt,
            state: () => K.state, info: () => K.info, files: () => K.files, result, paintUnit, fontMode, painter: P };
  head();
})();
