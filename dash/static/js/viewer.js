/* _dgist experiment dashboard - tab "뷰어" (2026-11-14): the four screens of https://<host>:8000/viewer inside the dashboard.
   Pictures and status come over ONE stream from the dashboard's own server (/api/viewer/stream, viewer.py: it reads the VA and the non-VA
   service over loopback, the way one /viewer browser does). The verdicts are the services'; this file shows them with the viewer page's
   rules: boxes on the VA picture, a picture that stands still while the QoS is missed, badge, dimming, the figures under the picture.
   The stream is open only while the tab is shown or a capture includes the screens. Nothing here reaches a UE: no radio downlink is added.
   The screens are always the LIVE ones - a saved measurement holds no pictures, so Replay does not change them. */
'use strict';
(function () {
  const D = window.D, S = D.S;
  const PALETTE = ['#7aa2ff', '#ffb547', '#27c57a', '#ff6b9d', '#4dd4e0', '#c084fc', '#f97316', '#a3e635'], colorCache = {};
  function colorOf(name) {
    if (!(name in colorCache)) { let h = 0; for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0; colorCache[name] = PALETTE[h % PALETTE.length]; }
    return colorCache[name];
  }
  function roundRect(g, x, y, w, h, r) {
    g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r); g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
  }
  function h(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  function fmt(x, d) { return (x === null || x === undefined || isNaN(x)) ? '-' : Number(x).toFixed(d === undefined ? 0 : d); }
  function ms(v) { v.appendChild(h('small', '', 'ms')); }
  // the viewer page's constants, from the live configuration (never from a replayed one: the screens are live)
  function C() {
    const m = S.meta || {}, v = m.viewer || {}, va = m.va || {}, nv = m.nonva || {};
    return { DEADLINE: va.deadline_ms || m.deadline_ms || 200, DL: v.dl_ms === undefined ? 20 : v.dl_ms, CONF: v.display_conf === undefined ? 0.4 : v.display_conf, FPS: va.target_fps || 15,
             FREEZE: v.freeze !== false, BADGE: v.badge !== false, REC_BADGE: v.rec_badge !== false, MODEL: v.model_text || va.model || '-', NVPORT: nv.port || 8001,
             relay: !!v.relay, va: !!v.va, nv: !!v.nv };
  }
  const V = { root: null, grid: null, note: null, sub: null, sig: null, panes: [], byKey: {}, holds: new Set(), open: false, up: false, openAt: 0, ac: null, closeT: 0, timer: 0, rxAt: 0, fitKey: '', c: C() };

  // ------------------------------------------------------------------ panes
  function shell(key, kind, ue, name, sub, figs, five) {
    const pane = h('div', 'vpane'), top = h('div', 'vtop'), title = h('div', 'vue', name), tspan = h('span', '', sub); title.appendChild(tspan);
    const chip = h('div', 'vchip', '대기'); top.appendChild(title); top.appendChild(chip);
    const stage = h('div', 'vstage'), canvas = document.createElement('canvas'); if (kind === 'nonva') canvas.className = 'fit';
    canvas.width = kind === 'va' ? 1920 : 1280; canvas.height = kind === 'va' ? 1080 : 720;
    const badge = h('div', 'vbadge'), badgeMain = h('div', '', ''), badgeSub = h('small', '', ''); badge.appendChild(badgeMain); badge.appendChild(badgeSub);
    const wait = h('div', 'vwait show', 'UE 대기 중'); stage.appendChild(canvas); stage.appendChild(badge); stage.appendChild(wait);
    const metrics = h('div', 'vmetrics' + (five ? ' five' : '')), tiles = {};
    for (const [k, label] of figs) { const t = h('div', 'vm'), kEl = h('div', 'vk', label), v = h('div', 'vv', '-'); t.appendChild(kEl); t.appendChild(v); tiles[k] = { box: t, v, k: kEl }; metrics.appendChild(t); }
    const foot = h('div', 'vfoot'), footL = h('div', '', '-'), footR = h('div', '', '-'); foot.appendChild(footL); foot.appendChild(footR);
    pane.appendChild(top); pane.appendChild(stage); pane.appendChild(metrics); pane.appendChild(foot);
    return { key, kind, ue, name, pane, tspan, chip, stage, canvas, ctx: canvas.getContext('2d'), badge, badgeMain, badgeSub, wait, tiles, footL, footR, next: null, busy: false, rec: null, onPic: null };
  }
  function makeVa(key, ue) {
    const p = shell(key, 'va', ue, 'VA ' + ue, '', [['ul', '상향 (UL)'], ['ai', '추론 (AI)'], ['dl', '하향 (고정)'], ['sum', '합계'], ['fps', '표시 fps'], ['pct', 'QoS 충족률 (최근 100)']], false);
    return Object.assign(p, { meta: null, lastDrawAt: 0, draws: [], relayOk: false });
  }
  function makeNv(key, ue) {
    const p = shell(key, 'nonva', ue, 'non-VA ' + ue, '720p 영상 중계 · 추론 없음', [['fps', '수신 fps'], ['mbps', '상향 Mbps'], ['kb', '프레임 KB'], ['ul', '상향 지연 (UL)'], ['pct', 'QoS 충족률 (최근 100)']], true);
    return Object.assign(p, { active: false, lastFrameId: -1, lastChangeAt: 0, q: null, qAt: 0, lastPicAt: 0, statsOk: false, seen: false, relayFps: 15, waitNone: false });
  }
  function labels() {   // the texts that carry configuration values
    const c = V.c = C();
    for (const p of V.panes) {
      if (p.kind === 'va') {
        p.tspan.textContent = 'FHD · YOLO ' + c.MODEL + ' · dApp (cuPHY 안)'; p.tiles.sum.k.textContent = '합계 / ' + c.DEADLINE + ' ms';
        const t = fmt(c.DL); if (p.tiles.dl.v.firstChild === null || p.tiles.dl.v.firstChild.nodeValue !== t) { p.tiles.dl.v.textContent = t; ms(p.tiles.dl.v); }
      } else p.footR.textContent = 'non-VA 서비스 :' + c.NVPORT;
    }
    if (V.sub) V.sub.textContent = 'VA: UE 카메라 → 5G 상향 → cuPHY 안의 YOLO(' + c.MODEL + ') → 검출 결과. QoS ' + c.DEADLINE + ' ms = 상향(실측) + 추론(실측) + 하향 ' + fmt(c.DL) + ' ms(고정), 미달 결과는 화면을 갱신하지 않음. ' +
      'non-VA: 720p 영상 중계(추론 없음)' + (V.nvSub || '') + '. 화면은 https://' + location.hostname + ':' + ((S.meta && S.meta.va && S.meta.va.port) || 8000) + '/viewer 와 같은 것이며 서버 안에서만 받아 옵니다 (UE 쪽 트래픽 없음).';
  }

  // ------------------------------------------------------------------ VA pane (viewer page: draw, applyMeta, tick)
  function draw(p, bmp, meta, at) {
    const c = p.canvas, conf0 = V.c.CONF;
    if (c.width !== bmp.width || c.height !== bmp.height) { c.width = bmp.width; c.height = bmp.height; }
    const g = p.ctx; g.drawImage(bmp, 0, 0); bmp.close();
    const s = Math.max(1, c.width / 960);
    for (const d of meta.dets || []) {
      const conf = Number(d.confidence); if (conf < conf0) continue;
      const x = d.x1, y = d.y1, w = d.x2 - d.x1, hh = d.y2 - d.y1, col = colorOf(String(d.class));
      g.lineWidth = 3 * s; g.strokeStyle = col; g.lineJoin = 'round'; g.strokeRect(x, y, w, hh);
      const label = d.class + ' ' + Math.round(conf * 100) + '%';
      g.font = '600 ' + Math.round(15 * s) + "px -apple-system, 'Segoe UI', Roboto, sans-serif";
      const tw = g.measureText(label).width + 12 * s, th = 22 * s, ly = (y - th - 2 * s >= 0) ? y - th - 2 * s : y + 2 * s;
      g.fillStyle = col; roundRect(g, x, ly, tw, th, 5 * s); g.fill();
      g.fillStyle = '#0b0f14'; g.textBaseline = 'middle'; g.fillText(label, x + 6 * s, ly + th / 2);
    }
    const now = performance.now(); p.lastDrawAt = at; if (now - at < 1000) p.draws.push(at); while (p.draws.length && p.draws[0] < now - 1000) p.draws.shift();
    if (p.onPic) p.onPic(p);
  }
  function applyMeta(p, meta) {
    p.meta = meta;
    const t = p.tiles, bad = meta.verdict === 'missed' || meta.verdict === 'fail';
    t.ul.v.textContent = meta.ul_known ? fmt(meta.ul_ms) : '?'; t.ai.v.textContent = fmt(meta.ai_ms); t.sum.v.textContent = fmt(meta.sum_ms);
    ms(t.ul.v); ms(t.ai.v); ms(t.sum.v);
    t.sum.box.className = 'vm ' + (meta.verdict === 'none' ? '' : (bad ? 'bad' : 'ok'));
    t.pct.v.textContent = meta.met_pct === null || meta.met_pct === undefined ? '-' : fmt(meta.met_pct, 0) + '%';
    t.pct.box.className = 'vm ' + (meta.met_pct === null || meta.met_pct === undefined ? '' : (meta.met_pct >= 90 ? 'ok' : (meta.met_pct < 70 ? 'bad' : '')));
    p.footL.textContent = '모델 ' + (meta.model || '-') + (meta.sm ? ' · ' + meta.sm + ' SM' : '') + ' · 결과 ' + meta.results + ' · 충족 ' + meta.met + ' · 미달 ' + meta.missed + (meta.failed ? ' · 실패 ' + meta.failed : '') + (meta.stale ? ' · 순서 뒤짐 ' + meta.stale : '');
    p.footR.textContent = (meta.mbps ? '상향 ' + fmt(meta.mbps, 1) + ' Mbps · ' + fmt(meta.req_s, 1) + ' req/s · ' + fmt(meta.kb) + ' KB · ' : '') + '화면 #' + meta.seq + (meta.w ? ' · ' + meta.w + 'x' + meta.h : '');
  }
  function set(e, text) { if (e.textContent !== text) e.textContent = text; }
  function cls(e, name) { if (e.className !== name) e.className = name; }
  function vaTick(p, now) {
    const c = V.c, meta = p.meta, held = p.lastDrawAt ? now - p.lastDrawAt : 0, starting = !V.up && now - V.openAt < 2500, connected = V.up && p.relayOk;
    while (p.draws.length && p.draws[0] < now - 1000) p.draws.shift();
    set(p.tiles.fps.v, p.lastDrawAt ? String(p.draws.length) : '-');
    const active = !!(meta && meta.active) && connected;
    cls(p.wait, 'vwait' + ((!active && !(meta && meta.seq)) || !connected ? ' show' : ''));
    set(p.wait, connected ? 'UE 대기 중' : (starting ? '연결 중…' : (V.up ? 'VA 서비스 연결 끊김' : '서버 연결 끊김')));
    const missed = active && !!meta && (meta.verdict === 'missed' || meta.verdict === 'fail') && c.FREEZE;                    // the UE is sending, its results miss
    const stalled = !!(meta && meta.seq) && p.lastDrawAt > 0 && held > Math.max(1000, c.DEADLINE + 3000 / Math.max(1, c.FPS));   // no new picture for a while
    const frozen = missed || stalled;
    cls(p.stage, 'vstage' + (frozen ? ' frozen' : '') + (c.BADGE ? ' dim' : ''));
    if (c.BADGE && frozen && meta && meta.seq) {
      cls(p.badge, 'vbadge show' + (missed ? '' : ' grey'));
      if (missed) {
        set(p.badgeMain, (meta.verdict === 'fail' ? '추론 실패 (' + (meta.reason || 'fail') + ')' : 'QoS 미달') + ' · 화면 유지 ' + (held / 1000).toFixed(2) + ' s');
        set(p.badgeSub, meta.verdict === 'fail' ? '연속 ' + meta.streak + '회' :
          'UL ' + fmt(meta.ul_ms) + ' + AI ' + fmt(meta.ai_ms) + ' + DL ' + fmt(meta.dl_ms) + ' = ' + fmt(meta.sum_ms) + ' ms > ' + meta.deadline_ms + ' ms · 연속 ' + meta.streak + '회');
      } else {
        set(p.badgeMain, (active ? '갱신 없음' : 'UE 송신 없음') + ' · ' + (held / 1000).toFixed(1) + ' s');
        set(p.badgeSub, active ? '새 결과가 아직 없습니다' : '마지막 결과 ' + (meta.since_last_ms !== null && meta.since_last_ms !== undefined ? (meta.since_last_ms / 1000).toFixed(0) + ' s 전' : '-'));
      }
    } else cls(p.badge, 'vbadge');
    if (!connected) { set(p.chip, starting ? '연결 중' : '연결 끊김'); cls(p.chip, 'vchip' + (starting ? '' : ' bad')); }
    else if (!active) { set(p.chip, meta && meta.seq ? 'UE 정지' : '대기'); cls(p.chip, 'vchip'); }
    else if (missed) { set(p.chip, 'QoS 미달 · 정지'); cls(p.chip, 'vchip bad'); }
    else { set(p.chip, 'QoS 충족 · 실시간'); cls(p.chip, 'vchip ok'); }
  }

  // ------------------------------------------------------------------ non-VA pane (viewer page: nonvaLoop, nvQos, nvTick)
  function nvStats(p, d) {
    p.seen = true;
    if (d.ok) {
      p.active = !!d.active; p.statsOk = true; p.relayFps = d.viewer_target_fps || 15;
      if (d.frame_id !== p.lastFrameId) { p.lastFrameId = d.frame_id; p.lastChangeAt = Date.now(); }
      p.tiles.fps.v.textContent = fmt(d.receive_fps, 1); p.tiles.mbps.v.textContent = fmt(d.receive_mbps, 1); p.tiles.kb.v.textContent = fmt(d.frame_size_kb);
      if (d.qos) nvQos(p, d.qos);
      else { p.tiles.ul.v.textContent = fmt(d.ul_latency_ms); ms(p.tiles.ul.v); }                       // a non-VA service without the verdict
      const q = p.q;
      p.footL.textContent = '프레임 #' + d.frame_id + ' · 중계 ' + fmt(d.viewer_target_fps) + ' fps' + (q ? ' · 충족 ' + q.met + ' · 미달 ' + q.missed + (q.stale ? ' · 순서 뒤짐 ' + q.stale : '') : '');
      if (!q) { p.chip.textContent = p.active ? '송출 중' : (d.frame_id > 0 ? 'UE 정지' : '대기'); p.chip.className = 'vchip' + (p.active ? ' ok' : ''); }   // with a verdict the chip is nvTick's
      const none = !!(q && q.freeze && q.shown_seq === 0 && q.qseq > 0);                               // frames arrive, but none has met the deadline yet
      p.wait.className = 'vwait' + (none || !(p.active || d.frame_id > 0) ? ' show' : '');
      p.wait.textContent = none ? 'QoS를 충족한 프레임이 아직 없습니다' : 'UE 대기 중'; p.waitNone = none;
    } else {
      p.statsOk = false; p.chip.textContent = 'non-VA 서비스 없음'; p.chip.className = 'vchip bad'; p.wait.className = 'vwait show'; p.wait.textContent = 'non-VA 세션 없음';
    }
  }
  function nvQos(p, q) {
    if (p.q && q.sid === p.q.sid && q.qseq < p.q.qseq) return;
    p.q = q; p.qAt = performance.now();
    const t = p.tiles;
    t.ul.k.textContent = '상향 지연 / ' + q.deadline_ms + ' ms';
    t.ul.v.textContent = fmt(q.ul_ms); ms(t.ul.v);
    t.ul.box.className = 'vm ' + (q.verdict === 'met' ? 'ok' : (q.verdict === 'missed' ? 'bad' : ''));
    t.pct.v.textContent = q.met_pct === null || q.met_pct === undefined ? '-' : fmt(q.met_pct, 0) + '%';
    t.pct.box.className = 'vm ' + (q.met_pct === null || q.met_pct === undefined ? '' : (q.met_pct >= 90 ? 'ok' : (q.met_pct < 70 ? 'bad' : '')));
    if (!V.nvSub) { V.nvSub = ', QoS ' + q.deadline_ms + ' ms = 상향(실측)' + (q.freeze ? ', 미달 프레임은 화면을 갱신하지 않음' : ''); labels(); }
  }
  function nvTick(p, now) {
    const c = V.c, q = p.q, gone = !V.up, starting = gone && now - V.openAt < 2500;
    if (gone || !p.seen) {                                                                            // this page lost its stream, or nothing was received yet
      set(p.chip, starting || !gone ? '연결 중' : '연결 끊김'); cls(p.chip, 'vchip' + (starting || !gone ? '' : ' bad'));
      if (!p.lastPicAt) { cls(p.wait, 'vwait show'); set(p.wait, starting || !gone ? '연결 중…' : '서버 연결 끊김'); }
      if (!q) return;
    }
    if (!q) return;                                                                                  // a non-VA service without the verdict: nvStats sets the chip
    const pic = p.lastPicAt > 0;
    if (p.waitNone && q.shown_seq > 0) { p.waitNone = false; p.wait.className = 'vwait'; }           // the first frame that met the deadline is here
    const held = !pic ? 0 : now - p.lastPicAt;
    const active = p.active && p.statsOk && !gone;
    const missed = active && q.verdict === 'missed' && !!q.freeze;                                    // the UE is sending, its frames arrive too late
    const stalled = pic && held > Math.max(1000, q.deadline_ms + 3000 / Math.max(1, p.relayFps));       // no new picture for a while
    const frozen = missed || stalled;
    cls(p.stage, 'vstage' + (frozen ? ' frozen' : '') + (c.BADGE ? ' dim' : ''));
    if (c.BADGE && frozen && pic) {
      cls(p.badge, 'vbadge show' + (missed ? '' : ' grey'));
      if (missed) {
        set(p.badgeMain, 'QoS 미달 · 화면 유지 ' + (held / 1000).toFixed(2) + ' s');
        set(p.badgeSub, (q.ul_ms === null ? 'UL 측정 불가' : 'UL ' + fmt(q.ul_ms) + ' ms > ' + q.deadline_ms + ' ms') + ' · 연속 ' + q.streak + '회');
      } else {
        set(p.badgeMain, (active ? '갱신 없음' : 'UE 송신 없음') + ' · ' + (held / 1000).toFixed(1) + ' s');
        set(p.badgeSub, active ? '새 프레임이 아직 없습니다' : (p.lastChangeAt ? '마지막 프레임 ' + ((Date.now() - p.lastChangeAt) / 1000).toFixed(0) + ' s 전' : ''));
      }
    } else cls(p.badge, 'vbadge');
    if (gone) return;
    if (!p.statsOk) { set(p.chip, 'non-VA 서비스 없음'); cls(p.chip, 'vchip bad'); }
    else if (!p.active) { set(p.chip, p.lastFrameId > 0 ? 'UE 정지' : '대기'); cls(p.chip, 'vchip'); }
    else if (q.verdict === 'missed') { set(p.chip, q.freeze ? 'QoS 미달 · 정지' : 'QoS 미달'); cls(p.chip, 'vchip bad'); }
    else { set(p.chip, 'QoS 충족 · 실시간'); cls(p.chip, 'vchip ok'); }
  }
  function tick() { const now = performance.now(); for (const p of V.panes) (p.kind === 'va' ? vaTick : nvTick)(p, now); }

  // ------------------------------------------------------------------ pictures: one decode at a time per pane, of those that arrive meanwhile only the newest is kept
  async function paint(p) {
    if (p.busy) return;
    p.busy = true;
    while (p.next) {
      const it = p.next; p.next = null;
      try {
        const bmp = await createImageBitmap(new Blob([it.jpeg], { type: 'image/jpeg' }));
        if (p.kind === 'va') draw(p, bmp, it.meta, it.at);
        else {
          const c = p.canvas; if (c.width !== bmp.width || c.height !== bmp.height) { c.width = bmp.width; c.height = bmp.height; }
          p.ctx.drawImage(bmp, 0, 0); bmp.close(); p.lastPicAt = it.at; if (p.onPic) p.onPic(p);
        }
      } catch (e) {}
    }
    p.busy = false;
  }
  function onRecord(hd, body) {
    if (hd.k === 'hello' || hd.k === 'ka') return;
    const p = V.byKey[hd.pane]; if (!p) return;
    const now = performance.now(), age = Number(hd.age) || 0;                                          // age: a record the relay already had when this page joined
    if (p.kind === 'va') {
      if (hd.k === 'c') { p.relayOk = !!hd.ok; if (!hd.ok) { p.next = null; } }
      else if (hd.k === 'p' || hd.k === 'm') {
        const meta = hd.meta || {}; p.relayOk = true;
        if (hd.k === 'p' && body.length) { p.next = { jpeg: body, meta, at: now - age }; paint(p); }
        if (meta.stat_seq !== undefined) applyMeta(p, meta);
      }
    } else {
      if (hd.k === 's') nvStats(p, hd.d || { ok: false });
      else if (hd.k === 'p' || hd.k === 'm') {
        const meta = hd.meta || null;
        if (meta) nvQos(p, meta);
        if (hd.k === 'p' && body.length && (!meta || meta.pic !== false)) { p.next = { jpeg: body, at: now - (meta && meta.held_ms ? meta.held_ms : 0) - age }; paint(p); }
      }
    }
  }

  // ------------------------------------------------------------------ the stream: [u32 header length][header JSON][u32 body length][body]
  async function reader() {
    const dec = new TextDecoder('utf-8');
    while (V.open) {
      const ac = V.ac = new AbortController(); let got = false;
      try {
        const r = await fetch('/api/viewer/stream', { cache: 'no-store', signal: ac.signal });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const rd = r.body.getReader(); let buf = new Uint8Array(1 << 20), len = 0;
        for (;;) {
          const { done, value } = await rd.read();
          if (done) break;
          if (!got) { got = true; V.up = true; }
          V.rxAt = performance.now();
          if (len + value.length > buf.length) { const b = new Uint8Array(Math.max(2 * buf.length, len + value.length)); b.set(buf.subarray(0, len)); buf = b; }
          buf.set(value, len); len += value.length;
          let o = 0; const dv = new DataView(buf.buffer);
          for (;;) {
            if (len - o < 8) break;
            const hl = dv.getUint32(o); if (len - o < 8 + hl) break;
            const bl = dv.getUint32(o + 4 + hl), end = o + 8 + hl + bl; if (len < end) break;
            let hd = null; try { hd = JSON.parse(dec.decode(buf.subarray(o + 4, o + 4 + hl))); } catch (e) {}
            if (hd) { try { onRecord(hd, buf.slice(o + 8 + hl, end)); } catch (e) { console.error(e); } }
            o = end;
          }
          if (o) { buf.copyWithin(0, o, len); len -= o; }
          tick();
        }
      } catch (e) {}
      if (V.ac === ac) V.ac = null;
      V.up = false; for (const p of V.panes) { if (p.kind === 'va') p.relayOk = false; else p.seen = false; }
      if (!V.open) break;
      tick(); await new Promise(res => setTimeout(res, 700));
    }
    V.reading = false;
  }
  function openStream() {
    if (V.closeT) { clearTimeout(V.closeT); V.closeT = 0; }
    if (V.open || !V.panes.length) return;
    V.open = true; V.openAt = performance.now(); V.rxAt = V.openAt;
    if (!V.timer) V.timer = setInterval(() => {
      tick();
      if (V.up && V.ac && performance.now() - V.rxAt > 15000) V.ac.abort();                            // the server sends a keep-alive record every 5 s
    }, 100);
    if (!V.reading) { V.reading = true; reader(); }
  }
  function closeStream() {
    V.open = false; V.up = false; if (V.ac) V.ac.abort();
    if (V.timer) { clearInterval(V.timer); V.timer = 0; }
  }
  function hold(key, on) {   // 'tab' = the tab is shown, 'cap' = a capture includes the screens
    if (on) V.holds.add(key); else V.holds.delete(key);
    if (V.holds.size) openStream();
    else if (V.open && !V.closeT) V.closeT = setTimeout(() => { V.closeT = 0; if (!V.holds.size) closeStream(); }, 3000);
  }

  // ------------------------------------------------------------------ layout: both rows on the screen when the window allows it
  function fit() {
    const g = V.grid; if (!g || !g.isConnected || S.tab !== 'view' || !V.panes.length) return;
    const W = g.parentElement.clientWidth, y0 = g.getBoundingClientRect().top + window.scrollY, H = window.innerHeight - y0 - 22, n = V.panes.length;
    const key = W + ' ' + H + ' ' + n; if (key === V.fitKey) return;
    V.fitKey = key;
    let bar = 0; for (const p of V.panes) bar = Math.max(bar, p.pane.offsetHeight - p.stage.offsetHeight);
    const gap = 14; let cols = 1, sh = 0;
    for (const c of n > 2 ? [2, n] : [n]) {                 // 2 x 2 like /viewer, or one row when the window is wide and low
      const rows = Math.ceil(n / c), wMax = (W - (c - 1) * gap) / c - 2, x = Math.floor(Math.min((H - (rows - 1) * gap) / rows - bar - 2, wMax * 9 / 16));
      if (x > sh) { sh = x; cols = c; }
    }
    if (!(sh >= 200) || W < 900) { g.classList.remove('fitv'); return; }
    g.classList.add('fitv'); g.style.setProperty('--vsh', sh + 'px'); g.style.setProperty('--vpw', (Math.floor(sh * 16 / 9) + 2) + 'px'); g.style.setProperty('--vcols', cols);
  }

  function make() {
    const c = C(); V.sig = [c.relay, c.va, c.nv].join();
    if (V.open) closeStream();
    V.panes = []; V.byKey = {}; V.fitKey = ''; V.nvSub = '';
    const root = V.root = h('div', 'vw');
    V.note = h('div', 'vnote', 'Replay 중입니다. 아래 화면은 저장된 기록이 아니라 지금의 실시간 화면입니다 (뷰어 영상은 서버에 저장되지 않습니다).'); V.note.hidden = true; root.appendChild(V.note);
    if (!c.relay || !(c.va || c.nv)) {
      root.appendChild(h('div', 'empty', S.meta && S.meta.sim ? '시뮬레이션 모드에는 뷰어 화면이 없습니다 (실제 VA / non-VA 서비스의 화면만 보여 줍니다).' :
        '뷰어 중계가 꺼져 있습니다 (.env DASH_VIEWER=0 또는 VA_VIEWER=0). 켠 뒤 ./dash.sh restart 하세요.'));
      V.grid = null; V.sub = null; return;
    }
    V.sub = h('div', 'vsub', ''); root.appendChild(V.sub);
    const g = V.grid = h('div', 'vgrid'); root.appendChild(g);
    if (c.va) for (const [k, ue] of [['va1', 'ue1'], ['va2', 'ue2']]) V.panes.push(makeVa(k, ue));
    if (c.nv) for (const [k, ue] of [['nv1', 'ue1'], ['nv2', 'ue2']]) V.panes.push(makeNv(k, ue));
    for (const p of V.panes) { V.byKey[p.key] = p; g.appendChild(p.pane); }
    labels();
  }
  function ensure() {
    const c = C(), sig = [c.relay, c.va, c.nv].join();
    if (!V.root || (sig !== V.sig && !V.panes.some(p => p.rec))) make();
  }
  function shown(on) { ensure(); hold('tab', !!on && V.panes.length > 0); if (on) { V.fitKey = ''; requestAnimationFrame(fit); } }

  D.views = D.views || {};
  D.views.view = {
    build(host) { ensure(); host.appendChild(V.root); shown(S.tab === 'view'); },                       // the root outlives a rebuild of the tabs: pictures and state stay
    update() { if (!V.root) return; labels(); V.note.hidden = !S.rp; fit(); },
  };
  D.viewer = {
    shown, hold, ensure,
    panes: () => { ensure(); return V.panes; },
    conf: () => V.c,
    up: () => V.up,
  };
  window.addEventListener('resize', () => { V.fitKey = ''; fit(); });
})();
