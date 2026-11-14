/* _dgist experiment dashboard - the six tabs (2026-11-12). Each view: build(host, meta) once, update(F) per tick, scope(F) per scope frame.
   Value rule on every tile: while a measurement runs the big number is the cumulative mean since "측정 시작" and the line below shows the live
   value; without a measurement the big number is the live value. */
'use strict';
(function () {
  const D = window.D, el = D.el, f = D.f, fu = D.fu, num = D.num, pk = D.pk;
  const V = D.views = {};
  const cvar = c => 'var(' + c + ')';
  const UEK = k => D.UE.find(u => u.k === k);
  const pctf = v => num(v) ? 100 * v : null;
  const px = h => typeof h === 'number' ? h + 'px' : h;
  const box = h => el('div', { style: { flex: '0 0 auto', height: px(h) } });      // a chart host of a fixed height

  // a series reading tick.<base>.<key> (live) and tick.<base>.c.<key> (cumulative mean)
  function ser(label, color, base, key, extra) {
    const a = base.split('.');
    const get = (t, c) => { let o = t; for (const k of a) { if (o == null) return null; o = o[k]; } if (c) o = o && o.c; return o == null ? null : o[key]; };
    return Object.assign({ label, color, inst: t => get(t, false), cum: t => get(t, true) }, extra || {});
  }
  function chartCard(span, title, sub, opt, h) {
    const c = D.card(span, title, sub), lg = el('div.legend'), host = el('div', { style: { flex: '1 1 auto', minHeight: px(h || 240) } });
    c.right.appendChild(lg); c.add(host); opt.legend = lg; c.chart = new D.TimeChart(host, opt); return c;
  }
  function scopeLegend(meta) {
    const e = el('div.legend', { style: { marginTop: '10px' } });
    const items = meta && meta.mode === 'baseline'
      ? [['--c3w', 'L1 전용 (MIG 4g)'], ['--c1', 'YOLO 추론 중'], ['--c1w', 'YOLO 전용 · 유휴 (MIG 3g)'], ['--unus', '미할당']]
      : [['--c3', 'PUSCH'], ['--c4', 'PDSCH'], ['--order', 'ORDER'], ['--c1', 'YOLO 실행'], ['--c1w', 'YOLO 제공 · 미사용'], ['--unus', '사용 불가']];
    for (const [c, l] of items) e.appendChild(D.legendItem(c, l, 'box').el);
    return e;
  }
  function ueHead(u) {
    const id = el('span.id'), tg = el('span.tag');
    const e = el('div.ueh', el('span.dot', { style: { background: cvar(u.color) } }), el('span.nm', u.name), id, el('div.r', tg));
    return {
      el: e, set(x, mu, brief) {
        const rn = (x && x.rnti) || (mu && mu.rnti);
        D.txt(id, brief ? '' : [rn ? 'RNTI ' + rn : 'RNTI 미확인', mu && mu.ip, mu && mu.imsi ? 'IMSI …' + String(mu.imsi).slice(-6) : null].filter(Boolean).join('  ·  '));
        if (x && x.on) { D.txt(tg, '● 전송 중'); D.cls(tg, 'tag good'); }
        else if (x && x.sync) { D.txt(tg, '○ 연결 · 전송 없음'); D.cls(tg, 'tag'); }
        else { D.txt(tg, '○ 미연결'); D.cls(tg, 'tag'); }
      },
    };
  }
  function chips(keys) {     // [[key, label]] -> {el, set(key, value)}
    const e = el('div.chips'), m = {};
    for (const [k, lab] of keys) { const b = el('b', '–'); m[k] = b; e.appendChild(el('span.chip', lab, b)); }
    return { el: e, set(k, v) { D.txt(m[k], v == null || v === '' ? '–' : String(v)); } };
  }
  const resName = (w, h) => !w || !h ? '–' : w + '×' + h + (h === 2160 ? ' (4K)' : h === 1080 && w === 1920 ? ' (FHD)' : h === 720 ? ' (HD)' : '');

  /* the 273-PRB bar: one block per service sized by its quota, filled by what its UEs use */
  function sliceBar() {
    const mk = svc => {
      const u = el('div.u'), segs = {};
      for (const x of D.UE) if (x.svc === svc) { segs[x.k] = el('div', { style: { background: cvar(x.color) } }); u.appendChild(segs[x.k]); }
      return { q: el('div.q', { style: { background: cvar(D.SVC[svc].weak) } }, u), u, segs, lab: el('div') };
    };
    const a = mk('va'), b = mk('nv'), rest = el('div.q', { style: { background: 'var(--hair)' } }), restl = el('div');
    const bar = el('div.slice', a.q, b.q, rest), lab = el('div.slicel', a.lab, restl, b.lab);
    const one = (p, quota, use, ues, name, tot) => {
      const g = num(quota) ? quota : (use || 0);
      p.q.style.flexGrow = Math.max(g, 0.001);
      p.q.style.flexBasis = '0';
      p.u.style.width = (num(quota) && quota > 0 ? Math.min(100, 100 * (use || 0) / quota) : 100) + '%';
      for (const k in p.segs) { p.segs[k].style.flexGrow = Math.max(ues[k] || 0, 0.001); p.segs[k].style.flexBasis = '0'; }
      D.fill(p.lab, num(quota) ? [name + ' 할당 ', ['b', f(quota, 0)], ' PRB (' + f(100 * quota / tot, 0) + ' %) · 사용 ', ['b', f(use, 1)]] : [name + ' 사용 ', ['b', f(use, 1)], ' PRB']);
      return g;
    };
    return {
      el: el('div', bar, lab),
      // d: {tot, va: {quota, use, ue: {va1, va2}}, nv: {...}}
      set(d) {
        const ga = one(a, d.va.quota, d.va.use, d.va.ue, 'VA', d.tot), gb = one(b, d.nv.quota, d.nv.use, d.nv.ue, 'Non-VA', d.tot), r = Math.max(0, d.tot - ga - gb);
        rest.style.flexGrow = r; rest.style.flexBasis = '0'; rest.style.display = r > 0.5 ? '' : 'none';
        const nod = !num(d.va.quota) && !num(d.nv.quota) && !num(d.va.use) && !num(d.nv.use);      // no radio source yet: unknown, not unused
        restl.style.display = r > 0.5 ? '' : 'none'; D.txt(restl, nod ? '무선 데이터 수신 대기 중' : '미사용 ' + f(r, 0) + ' PRB');
      },
    };
  }
  function sliceData(t, m, cum) {
    const g = (o, k) => !o ? null : cum ? (o.c ? o.c[k] : null) : o[k], tot = m.prb_total || 273, out = { tot };
    for (const s of ['va', 'nv']) {
      const ue = {}; let sum = 0, any = false;
      for (const u of D.UE) if (u.svc === s) { const v = g(t.ue[u.k], 'prb'); ue[u.k] = v; if (num(v)) { sum += v; any = true; } }
      const sv = g(t.svc[s], 'prb');
      out[s] = { quota: g(t.svc[s], 'quota'), use: num(sv) ? sv : (any ? sum : null), ue };
    }
    return out;
  }
  const QSRC = { gnb: 'gNB 로그 (E2 슬라이스 전환)', ppo: 'PPO xApp', moni: '모니터 xApp' };
  const PSRC = { ppo: 'PPO xApp 입력 (E2 KPM)', moni: '모니터 xApp', gnb: 'gNB 로그 표본 (~1 %)' };

  /* five boxes, the one in use lit */
  function modelBoxes(meta) {
    const e = el('div.mboxes'), m = {};
    for (const name of meta.models || []) {
      const sh = el('div.sh', '–'), bar = el('i'), box = el('div.mbox', el('div.m', name), el('div.p', 'mAP ' + f((meta.map || {})[name], 1)), sh, el('div.bar', bar));
      m[name] = { box, sh, bar }; e.appendChild(box);
    }
    return {
      el: e, set(cur, counts) {
        const tot = D.sum(counts);
        for (const k in m) {
          D.cls(m[k].box, 'mbox' + (k === cur ? ' lit' : ''));
          const p = tot > 0 ? 100 * (counts[k] || 0) / tot : null;
          D.txt(m[k].sh, p == null ? '–' : f(p, p > 0 && p < 99.95 ? 1 : 0) + ' %'); m[k].bar.style.width = (p || 0) + '%';
        }
      },
    };
  }
  function verdictTag(tg, v) { const d = D.VERDICT[v] || D.VERDICT.idle; D.txt(tg, d[2] + ' ' + d[0]); D.cls(tg, 'tag' + (d[1] ? ' ' + d[1] : '')); }

  // ================================================================== 종합
  V.ov = {
    build(host, meta) {
      const P = this.p = {};
      P.qos = D.tile('VA QoS 충족률', 'hero'); P.e2e = D.tile('VA E2E Latency'); P.thr = D.tile('셀 Uplink Throughput');
      P.req = D.tile('GPU YOLO 요청'); P.map = D.tile('정확도 유지율 (mAP)'); P.dur = D.tile('측정 시간');
      P.vtag = el('span.tag'); P.req.lb.appendChild(P.vtag);
      host.appendChild(el('div.tiles', P.qos.el, P.e2e.el, P.thr.el, P.req.el, P.map.el, P.dur.el));

      const c1 = chartCard('s7', 'VA E2E Latency', 'UE별 · 캡처부터 결과 표시까지', {
        unit: 'ms', series: [ser('VA UE1', '--c1', 'ue.va1', 'e2e'), ser('VA UE2', '--c2', 'ue.va2', 'e2e')], thresholds: [{ label: 'QoS 기한', get: m => m.deadline_ms }],
      }, 'calc(226px + max(0px, (100vh - 950px - var(--rb, 0px)) * 0.6))');
      P.c1 = c1.chart;
      const c2 = D.card('s5', 'GPU SM 슬롯 맵', meta.mode === 'baseline' ? 'MIG 고정 분할 · L1과 YOLO가 서로의 빈 SM을 쓰지 못함' : '슬롯마다 cuPHY 채널과 YOLO가 나눠 쓰는 132 SM'), sh = el('div', { style: { flex: '1 1 auto', minHeight: '200px' } });
      c2.add(sh, scopeLegend(meta)); P.scope = new D.Scope(sh, { mini: true });
      host.appendChild(el('div.grid', { style: { marginTop: '16px' } }, c1.el, c2.el));

      const c3 = D.card('s5', 'PRB 슬라이싱 · 서비스 Throughput'); P.sliceSub = el('span.s'); c3.head.insertBefore(P.sliceSub, c3.right);
      P.slice = sliceBar(); c3.add(P.slice.el);
      const lg = el('div.legend', { style: { margin: '14px 0 2px' } }), ch = el('div', { style: { flex: '1 1 auto', minHeight: 'calc(124px + max(0px, (100vh - 950px - var(--rb, 0px)) * 0.4))' } }); c3.add(lg, ch);
      P.c3 = new D.TimeChart(ch, { unit: 'Mbps', legend: lg, series: [ser('VA 서비스', '--c1', 'svc.va', 'mac'), ser('Non-VA 서비스', '--c3', 'svc.nv', 'mac')] });
      const c4 = D.card('s7', 'UE 현황'); P.ueSub = el('span.s'); c4.head.insertBefore(P.ueSub, c4.right);
      const uc = el('div.uecards'); P.ue = {};
      for (const u of D.UE) {
        const h = ueHead(u), v = el('span.v'), l = el('span.l'), un = el('span.u', 'ms');
        const kv = D.kv(u.svc === 'va' ? [['Throughput', 'thr'], ['PRB', 'prb'], ['SNR · MCS', 'snr'], ['UL BLER', 'bler'], ['모델 @ SM', 'model']] : [['Throughput', 'thr'], ['PRB', 'prb'], ['SNR · MCS', 'snr'], ['UL BLER', 'bler'], ['수신 FPS', 'fps']]);
        const cap = el('span', { style: { fontSize: '12.5px', color: 'var(--ink5)', fontWeight: 600 } }, u.svc === 'va' ? 'E2E' : 'UL Latency');
        P.ue[u.k] = { h, v, l, kv };
        uc.appendChild(el('div.uec', h.el, el('div', cap, el('div.main', v, un, l)), kv.el));
      }
      c4.add(uc);
      host.appendChild(el('div.grid', { style: { marginTop: '16px' } }, c3.el, c4.el));
    },
    update(F) {
      const t = F.cur, P = this.p, m = F.meta; if (!t) return;
      const va = t.svc.va || {}, nv = t.svc.nv || {}, g = t.gpu || {}, cell = t.cell || {}, rec = D.recNow, vc = va.c || {}, gc = g.c || {};
      P.qos.kpi(vc.qos, va.qos, 1, '%', ['기한 ', ['b', f(m.deadline_ms, 0) + ' ms'], ' 이내']);
      P.e2e.kpi(vc.e2e, va.e2e, 1, 'ms', ['UL ', ['b', f(pk(va, 'ul'), 1)], ' + Inference ', ['b', f(pk(va, 'ai'), 1)]]);
      P.thr.kpi((cell.c || {}).mac, cell.mac, 1, 'Mbps', ['VA ', ['b', f(pk(va, 'mac'), 1)], ' · Non-VA ', ['b', f(pk(nv, 'mac'), 1)]]);
      P.req.kpi(gc.req, g.req, 1, 'req/s', ['부하율 ', ['b', f(g.busy, 0) + ' %']]);
      verdictTag(P.vtag, g.verdict);
      const base = (m.map || {})[m.base_model], ret = v => num(v) && base ? 100 * v / base : null;
      P.map.kpi(ret(gc.map), ret(g.map), 1, '%', ['mAP ', ['b', f(pk(g, 'map'), 1)], ' / ' + f(base, 1) + ' (' + (m.base_model || '26x') + ')']);
      if (rec) { P.dur.text(D.mmss(t.el)).kind(null); D.fill(P.dur.sub, ['VA ', ['b', f(vc.n, 0)], ' 프레임 · Non-VA ', ['b', f((nv.c || {}).n, 0)], ' 프레임']); }
      else { P.dur.text('대기').kind(null); D.fill(P.dur.sub, ['“측정 시작”을 누르면 누적 평균을 기록합니다']); }
      P.c1.draw(F); P.c3.draw(F); P.scope.draw(F);
      const noq = !num(va.quota) && !num(nv.quota);
      D.txt(P.sliceSub, (rec ? '측정 평균' : '지금') + ' · ' + (noq ? '슬라이싱 없음 (PF 공유)' : '273 PRB 분할'));
      P.slice.set(sliceData(t, m, rec));
      D.txt(P.ueSub, rec ? '측정 시작 후 누적 평균' : '지금 (최근 1 초)');
      for (const u of D.UE) {
        const x = t.ue[u.k] || {}, p = P.ue[u.k], mu = (m.ues || {})[u.k];
        p.h.set(x, mu, true);
        if (u.svc === 'va') { D.setNum(p.v, pk(x, 'e2e'), 1); D.fill(p.l, ['QoS ', ['b', fu(pk(x, 'qos'), 1, '%')]]); p.kv.set('model', x.model ? x.model + ' @ ' + (x.sm || '–') : '–'); }
        else { D.setNum(p.v, pk(x, 'ul'), 1); D.fill(p.l, ['QoS ', ['b', fu(pk(x, 'qos'), 1, '%')], ' · ' + resName((m.nonva || {}).width, (m.nonva || {}).height)]); p.kv.set('fps', fu(pk(x, 'fps'), 1, 'fps')); }
        p.kv.set('thr', f(pk(x, 'mac'), 1), 'Mbps'); p.kv.set('prb', f(pk(x, 'prb'), 1));
        p.kv.set('snr', f(pk(x, 'snr'), 1) + ' dB · ' + f(pk(x, 'mcs'), 1)); p.kv.set('bler', fu(pctf(pk(x, 'bler')), 1, '%'));
      }
    },
    scope(F) { this.p.scope.draw(F); },
  };

  // ================================================================== VA 서비스
  V.va = {
    build(host, meta) {
      const P = this.p = { ue: {} };
      P.t = [D.tile('QoS 충족률', 'hero'), D.tile('E2E Latency'), D.tile('Uplink Latency'), D.tile('Inference Latency'), D.tile('서비스 Throughput'), D.tile('요청 FPS (2 UE 합)')];
      host.appendChild(el('div.tiles', P.t.map(x => x.el)));
      const g = el('div.grid', { style: { marginTop: '16px' } });
      for (const u of D.UE) {
        if (u.svc !== 'va') continue;
        const p = P.ue[u.k] = {}, c = el('div.card.s6');
        p.h = ueHead(u);
        p.chips = chips([['res', '화질'], ['q', 'JPEG Quality'], ['fps', 'FPS 목표 / 실제'], ['kb', '프레임'], ['thr', 'Throughput'], ['src', '입력']]);
        p.t = { ul: D.tile('Uplink Latency', 'in'), ai: D.tile('Inference Latency', 'in'), e2e: D.tile('E2E Latency', 'in'), qos: D.tile('QoS 충족률', 'in') };
        const lg = el('div.legend'), ch = box('clamp(150px, calc(100vh - 850px - var(--rb, 0px)), 320px)');
        p.chart = new D.TimeChart(ch, {
          unit: 'ms', legend: lg, thresholds: [{ label: 'QoS 기한', get: m => m.deadline_ms }],
          series: [ser('Uplink', '--c3', 'ue.' + u.k, 'ul'), ser('Inference', '--c1', 'ue.' + u.k, 'ai'), ser('E2E', '--total', 'ue.' + u.k, 'e2e', { heavy: true })],
        });
        p.bar = el('div.hbar.tall'); p.seg = {}; p.blg = el('div.legend', { style: { marginTop: '8px' } }); p.bl = {};
        for (const k of ['ul', 'wait', 'inf', 'srv', 'dl']) {
          p.seg[k] = el('div', { style: { background: cvar(D.LAT[k].color), flexBasis: '0' } }); p.bar.appendChild(p.seg[k]);
          p.bl[k] = D.legendItem(D.LAT[k].color, D.LAT[k].label, 'box'); p.blg.appendChild(p.bl[k].el);
        }
        p.barT = el('b'); p.barS = el('span.r'); p.mb = modelBoxes(meta); p.mbS = el('span.r');
        c.appendChild(p.h.el); c.appendChild(el('div', { style: { margin: '12px 0' } }, p.chips.el));
        c.appendChild(el('div.tiles.c4', p.t.ul.el, p.t.ai.el, p.t.e2e.el, p.t.qos.el));
        c.appendChild(el('div.rowl', el('span', 'Latency 추이'), el('span.r', lg))); c.appendChild(ch);
        c.appendChild(el('div.rowl', el('span', 'E2E 구성'), p.barT, p.barS)); c.appendChild(p.bar); c.appendChild(p.blg);
        c.appendChild(el('div.rowl', el('span', '선택된 모델'), p.mbS)); c.appendChild(p.mb.el);
        g.appendChild(c);
      }
      host.appendChild(g);
      host.appendChild(el('div.note', { style: { marginTop: '12px' } }, 'Inference Latency = 서버가 프레임을 받은 뒤 결과를 내보낼 때까지 (대기 + JPEG 디코드 + GPU 추론 + 후처리). E2E = 폰이 프레임을 캡처한 뒤 결과를 화면에 그릴 때까지 (폰이 보고한 값; 보고가 없는 프레임은 UL + Inference + 고정 DL로 추정하고 "추정"으로 표시).'));
    },
    update(F) {
      const t = F.cur, P = this.p, m = F.meta; if (!t) return;
      const va = t.svc.va || {}, c = va.c || {}, rec = D.recNow, mv = m.va || {};
      P.t[0].kpi(c.qos, va.qos, 1, '%', ['기한 ', ['b', f(m.deadline_ms, 0) + ' ms']]);
      P.t[1].kpi(c.e2e, va.e2e, 1, 'ms'); P.t[2].kpi(c.ul, va.ul, 1, 'ms'); P.t[3].kpi(c.ai, va.ai, 1, 'ms', rec && num(c.inf) ? ['GPU 추론 ', ['b', f(c.inf, 1) + ' ms']] : null);
      P.t[4].kpi(c.mbps, va.mbps, 1, 'Mbps', ['MAC ', ['b', f(pk(va, 'mac'), 1) + ' Mbps']]);
      P.t[5].kpi(rec && num(c.n) && t.el > 0 ? c.n / t.el : null, va.fps, 1, 'fps', rec ? ['누적 ', ['b', f(c.n, 0)], ' 프레임'] : null);
      for (const u of D.UE) {
        if (u.svc !== 'va') continue;
        const x = t.ue[u.k] || {}, xc = x.c || {}, p = P.ue[u.k], mu = (m.ues || {})[u.k];
        p.h.set(x, mu);
        p.chips.set('res', resName(x.w || (mv.camera || [])[0], x.h || (mv.camera || [])[1])); p.chips.set('q', mv.jpeg_quality);
        p.chips.set('fps', f(mv.target_fps, 0) + ' / ' + f(x.fps, 1)); p.chips.set('kb', fu(pk(x, 'kb'), 0, 'KB')); p.chips.set('thr', fu(pk(x, 'mbps'), 1, 'Mbps'));
        p.chips.set('src', x.src === 'est' ? '폰 (E2E 추정)' : x.src === 'file' ? '동영상 파일' : x.on ? '폰 카메라' : '–');
        const pc = a => (rec && a && num(a[1])) ? [' · p90 ', ['b', f(a[1], 0)], ' · p99 ', ['b', f(a[2], 0)]] : [];
        p.t.ul.kpi(xc.ul, x.ul, 1, 'ms'); if (rec) D.fill(p.t.ul.sub, ['지금 ', ['b', f(x.ul, 1)]].concat(pc(xc.ulp)));
        p.t.ai.kpi(xc.ai, x.ai, 1, 'ms'); if (rec) D.fill(p.t.ai.sub, ['지금 ', ['b', f(x.ai, 1)]].concat(pc(xc.aip)));
        p.t.e2e.kpi(xc.e2e, x.e2e, 1, 'ms'); if (rec) D.fill(p.t.e2e.sub, ['지금 ', ['b', f(x.e2e, 1)]].concat(pc(xc.e2ep)));
        p.t.qos.kpi(xc.qos, x.qos, 1, '%'); if (rec) D.fill(p.t.qos.sub, ['지금 ', ['b', fu(x.qos, 0, '%')], ' · ', ['b', f(xc.met, 0)], ' / ' + f(xc.n, 0)]);
        p.chart.draw(F);
        // where the E2E time goes
        const v = { ul: pk(x, 'ul'), wait: pk(x, 'wait'), inf: pk(x, 'inf'), dl: pk(x, 'dl') }, dec = pk(x, 'dec'), oth = pk(x, 'oth');
        v.srv = num(dec) || num(oth) ? (dec || 0) + (oth || 0) : null;
        let tot = 0; for (const k in v) tot += v[k] || 0;
        for (const k in p.seg) { p.seg[k].style.flexGrow = Math.max(v[k] || 0, 0.001); p.seg[k].title = D.LAT[k].label + ' ' + fu(v[k], 1, 'ms'); p.bl[k].set(f(v[k], 1), 'ms'); }
        D.txt(p.barT, tot > 0 ? f(tot, 1) + ' ms' : '–'); D.txt(p.barS, rec ? '측정 평균' : '지금');
        const counts = rec ? xc.models : x.models;
        p.mb.set(x.on ? x.model : null, counts || {});
        D.txt(p.mbS, (x.model ? '지금 ' + x.model + ' @ ' + (x.sm || '–') + ' SM · ' : '') + (rec ? '측정 중 사용 비율' : '최근 1 초 사용 비율') + (mv.model && mv.model !== 'auto' ? ' · 현재 VA_MODEL=' + mv.model + ' 고정' : ''));
      }
    },
  };

  // ================================================================== Non-VA 서비스
  V.nv = {
    build(host, meta) {
      const P = this.p = { ue: {} };
      P.t = [D.tile('QoS 충족률', 'hero'), D.tile('서비스 Throughput'), D.tile('MAC Throughput'), D.tile('Uplink Latency'), D.tile('수신 FPS (2 UE 합)'), D.tile('PRB 사용')];
      host.appendChild(el('div.tiles', P.t.map(x => x.el)));
      const g = el('div.grid', { style: { marginTop: '16px' } });
      for (const u of D.UE) {
        if (u.svc !== 'nv') continue;
        const p = P.ue[u.k] = {}, c = el('div.card.s6');
        p.h = ueHead(u);
        p.chips = chips([['res', '화질'], ['q', 'JPEG Quality'], ['fps', 'FPS 목표 / 실제'], ['kb', '프레임'], ['radio', 'SNR · MCS']]);
        p.t = { qos: D.tile('QoS 충족률', 'in'), ul: D.tile('Uplink Latency', 'in'), fps: D.tile('수신 FPS', 'in'), thr: D.tile('Throughput', 'in'), prb: D.tile('PRB 사용', 'in') };
        const lg1 = el('div.legend'), ch1 = box('clamp(160px, calc(50vh - 330px - var(--rb, 0px) / 2), 300px)'), lg2 = el('div.legend'), ch2 = box('clamp(160px, calc(50vh - 330px - var(--rb, 0px) / 2), 300px)');
        p.c1 = new D.TimeChart(ch1, { unit: 'ms', legend: lg1, series: [ser('Uplink Latency', u.color, 'ue.' + u.k, 'ul')], thresholds: [{ label: 'QoS 기한', get: m => m.nv_deadline_ms }] });
        p.c2 = new D.TimeChart(ch2, { unit: 'Mbps', legend: lg2, series: [ser('앱 수신 (JPEG)', u.color, 'ue.' + u.k, 'mbps'), ser('MAC', '--ink5', 'ue.' + u.k, 'mac')] });
        c.appendChild(p.h.el); c.appendChild(el('div', { style: { margin: '12px 0' } }, p.chips.el));
        c.appendChild(el('div.tiles.c5', p.t.qos.el, p.t.ul.el, p.t.fps.el, p.t.thr.el, p.t.prb.el));
        c.appendChild(el('div.rowl', el('span', 'Uplink Latency 추이'), el('span.r', lg1))); c.appendChild(ch1);
        c.appendChild(el('div.rowl', el('span', 'Throughput 추이'), el('span.r', lg2))); c.appendChild(ch2);
        g.appendChild(c);
      }
      host.appendChild(g);
      host.appendChild(el('div.note', { style: { marginTop: '12px' } }, 'Non-VA는 영상 프레임을 Uplink로만 보내는 서비스입니다 (추론 없음). Uplink Latency = 폰이 프레임을 보내기 시작한 뒤 서버가 다 받을 때까지. QoS 충족률 = Uplink Latency가 기한 (' + f(meta.nv_deadline_ms != null ? meta.nv_deadline_ms : 250, 0) + ' ms) 이내인 프레임의 비율 (뷰어가 화면을 멈추는 기준과 같음; Uplink 시간을 잴 수 없는 프레임은 미달). Throughput "앱 수신" = 서버가 받은 JPEG 바이트, "MAC" = gNB가 센 Uplink MAC 바이트 (헤더 · 재전송 포함).'));
    },
    update(F) {
      const t = F.cur, P = this.p, m = F.meta; if (!t) return;
      const nv = t.svc.nv || {}, c = nv.c || {}, rec = D.recNow, mn = m.nonva || {};
      P.t[0].kpi(c.qos, nv.qos, 1, '%', ['기한 ', ['b', f(m.nv_deadline_ms != null ? m.nv_deadline_ms : 250, 0) + ' ms'], ' (Uplink)']);
      P.t[1].kpi(c.mbps, nv.mbps, 1, 'Mbps', ['앱 수신 (JPEG 바이트)']); P.t[2].kpi(c.mac, nv.mac, 1, 'Mbps');
      P.t[3].kpi(c.ul, nv.ul, 1, 'ms'); P.t[4].kpi(rec && num(c.n) && t.el > 0 ? c.n / t.el : null, nv.fps, 1, 'fps', rec ? ['누적 ', ['b', f(c.n, 0)], ' 프레임'] : null);
      P.t[5].kpi(c.prb, nv.prb, 1, 'PRB', num(nv.quota) ? ['할당 ', ['b', f(pk(nv, 'quota'), 0) + ' PRB']] : ['슬라이싱 없음']);
      for (const u of D.UE) {
        if (u.svc !== 'nv') continue;
        const x = t.ue[u.k] || {}, xc = x.c || {}, p = P.ue[u.k];
        p.h.set(x, (m.ues || {})[u.k]);
        p.chips.set('res', resName(mn.width, mn.height)); p.chips.set('q', mn.jpeg_quality); p.chips.set('fps', f(mn.fps, 0) + ' / ' + f(x.fps, 1));
        p.chips.set('kb', fu(pk(x, 'kb'), 0, 'KB')); p.chips.set('radio', f(pk(x, 'snr'), 1) + ' dB · ' + f(pk(x, 'mcs'), 1));
        p.t.qos.kpi(xc.qos, x.qos, 1, '%'); if (rec && num(xc.met)) D.fill(p.t.qos.sub, ['지금 ', ['b', fu(x.qos, 0, '%')], ' · ', ['b', f(xc.met, 0)], ' / ' + f(xc.n, 0)]);
        p.t.ul.kpi(xc.ul, x.ul, 1, 'ms'); if (rec && xc.ulp && num(xc.ulp[1])) D.fill(p.t.ul.sub, ['지금 ', ['b', f(x.ul, 1)], ' · p90 ', ['b', f(xc.ulp[1], 0)], ' · p99 ', ['b', f(xc.ulp[2], 0)]]);
        p.t.fps.kpi(xc.fps, x.fps, 1, 'fps', rec ? ['누적 ', ['b', f(xc.n, 0)], ' 프레임'] : null);
        p.t.thr.kpi(xc.mbps, x.mbps, 1, 'Mbps', ['MAC ', ['b', f(pk(x, 'mac'), 1)]]);
        p.t.prb.kpi(xc.prb, x.prb, 1, 'PRB', ['BLER ', ['b', fu(pctf(pk(x, 'bler')), 1, '%')]]);
        p.c1.draw(F); p.c2.draw(F);
      }
    },
  };

  // ================================================================== GPU · 슬롯
  function heatmap() {
    const e = el('div.heat'); let key = '', cells = {};
    return {
      el: e,
      set(models, classes, counts, unit) {
        const seen = {}; for (const k in counts) { const s = Number(k.split('@')[1]); if (s) seen[s] = 1; }
        const cols = Array.from(new Set((classes || []).concat(Object.keys(seen).map(Number)))).sort((a, b) => a - b), kk = models.join() + '|' + cols.join();
        if (kk !== key) {
          key = kk; cells = {}; e.textContent = ''; e.style.gridTemplateColumns = '52px repeat(' + cols.length + ', minmax(0, 1fr))';
          e.appendChild(el('div.hh', '')); for (const s of cols) e.appendChild(el('div.hh', s + ' SM'));
          for (const mdl of models) { e.appendChild(el('div.rh', mdl)); for (const s of cols) { const c = el('div.c'); cells[mdl + '@' + s] = c; e.appendChild(c); } }
        }
        const tot = D.sum(counts); let mx = 0; for (const k in counts) if (counts[k] > mx) mx = counts[k];
        for (const k in cells) {
          const n = counts[k] || 0, p = tot ? 100 * n / tot : 0, a = n && mx ? 0.14 + 0.86 * n / mx : 0, c = cells[k];
          c.style.background = a ? 'rgba(' + D.cv('--seq0') + ',' + a.toFixed(3) + ')' : ''; D.cls(c, 'c' + (a > 0.5 ? ' hi' : ''));
          D.txt(c, n ? (p < 0.05 ? '<0.1' : f(p, 1)) + '%' : ''); c.title = k.replace('@', ' @ ') + ' SM  ·  ' + f(n, 0) + ' ' + unit + (tot ? '  (' + f(p, 1) + ' %)' : '');
        }
        return tot;
      },
    };
  }
  V.gpu = {
    build(host, meta) {
      const P = this.p = {};
      const c1 = D.card('s8', '슬롯별 GPU SM 배분', ''), sh = el('div', { style: { flex: '1 1 400px', minHeight: '400px' } });
      P.scSub = c1.subEl; P.scR = el('span.tag'); c1.right.appendChild(P.scR);
      c1.add(sh, scopeLegend(meta)); P.scope = new D.Scope(sh, {});
      const c2 = D.card('s4', 'YOLO 요청 부하', 'GPU가 받는 추론 요청과 여유'); P.vtag = el('span.tag'); c2.right.appendChild(P.vtag);
      P.req = D.tile('GPU가 받는 YOLO 요청', 'in'); P.busy = el('b'); P.fill = el('div.f'); P.busyS = el('span.r');
      P.kv = D.kv([['감당 가능한 요청률', 'cap'], ['대기열 (지금 · 평균)', 'queue'], ['대기 시간', 'wait'], ['GPU 추론 시간 (요청당)', 'inf'], ['거절된 요청', 'rej'], ['포화 · 주의 시간', 'sat'], ['YOLO SM (지금 · 평균)', 'sm'], ['GPU 사용률 · 전력 (NVML)', 'nvml']], 'big');
      c2.add(P.req.el, el('div.rowl', el('span', 'GPU 부하율'), P.busy, P.busyS),
        el('div.meter', { style: { marginTop: '22px' } }, P.fill, el('div.mk', { style: { left: '70%' } }, el('span', '주의 70')), el('div.mk', { style: { left: '90%' } }, el('span', '포화 90'))),
        el('div.meterl', el('span', '0'), el('span', '100 %')), el('div', { style: { marginTop: '14px' } }, P.kv.el));
      P.capNote = el('div.note'); c2.add(P.capNote);
      host.appendChild(el('div.grid', c1.el, c2.el));

      const sm = k => ({ inst: t => t.gpu && t.gpu.sm ? t.gpu.sm[k] : null, cum: t => t.gpu && t.gpu.c && t.gpu.c.sm ? t.gpu.c.sm[k] : null });
      const smx = fn => ({ inst: t => t.gpu && t.gpu.sm ? fn(t.gpu.sm) : null, cum: t => t.gpu && t.gpu.c && t.gpu.c.sm ? fn(t.gpu.c.sm) : null });
      const ord = meta.order_sm || 8;
      const c3 = meta.mode === 'baseline' ? chartCard('s5', 'YOLO 인스턴스 시간 점유율', 'MIG 3g ' + ((meta.baseline || {}).yolo_sm || 60) + ' SM · 추론 중인 시간의 비율 (요청률 × 추론 시간)', {
        unit: '%', digits: 1, yFixed: 100, yStep: 25, thresholds: [{ label: '포화 기준', get: () => 90 }],
        series: [{ label: 'GPU 부하율', color: '--c1', inst: t => t.gpu ? t.gpu.busy : null, cum: t => t.gpu && t.gpu.c ? t.gpu.c.busy : null }],
      }, 250) : chartCard('s5', 'SM 사용 추이', '슬롯 평균 · 합계 ' + (meta.gpu_sm || 132) + ' SM', {
        unit: 'SM', digits: 1, stack: true, yFixed: meta.gpu_sm || 132, yStep: (meta.gpu_sm || 132) / 4, emptyMsg: '슬롯 데이터 수신 대기 중 (L1 테넌트 슬롯 CSV)',
        series: [Object.assign({ label: 'PDSCH', color: '--c4' }, sm('pdsch')), Object.assign({ label: 'YOLO 실행', color: '--c1' }, sm('run')),
          Object.assign({ label: '제공 · 미사용', color: '--c1w' }, smx(s => Math.max(0, s.free - s.run))), Object.assign({ label: '사용 불가', color: '--unus' }, sm('unus')),
          Object.assign({ label: 'ORDER', color: '--order' }, smx(() => ord)), Object.assign({ label: 'PUSCH', color: '--c3' }, sm('pusch'))],
      }, 250);
      P.c3 = c3.chart;
      const c4 = D.card('s4', '모델 × SM 조합', ''); P.hmSub = c4.subEl; P.hm = heatmap(); P.hmNote = el('div.note'); c4.add(P.hm.el, P.hmNote);
      const c5 = D.card('', '정확도 (mAP)', (meta.base_model || '26x') + ' 고정 Baseline 대비');
      P.acc = D.tile('mAP 유지율', 'in'); P.akv = D.kv([['평균 mAP (요청 가중)', 'mean'], ['Baseline mAP (' + (meta.base_model || '26x') + ' 고정)', 'base'], ['정확도 감소율', 'loss']], 'big');
      c5.add(P.acc.el, el('div', { style: { marginTop: '12px' } }, P.akv.el), el('div.note', meta.map_src ? '모델별 mAP = ' + meta.map_src + '. 우리 FHD 파이프라인에서 실측한 값이 아닙니다.' : ''));
      const c6 = D.card('', '가상 카메라', 'GPU에 직접 넣는 추가 부하 (무선 미사용)'); P.vcTag = el('span.tag'); c6.right.appendChild(P.vcTag);
      P.vcChips = chips([['cams', '카메라'], ['fps', 'FPS'], ['model', '모델'], ['q', 'JPEG Quality'], ['dl', '기한'], ['inf', '동시 요청']]);
      P.vcKv = D.kv([['요청률', 'req'], ['QoS 충족률', 'met'], ['응답 시간 p50 · p90 · p99', 'rt']], 'big'); P.vcOff = el('div.empty', '가상 카메라가 꺼져 있습니다.');
      P.vcOn = el('div', P.vcChips.el, el('div', { style: { marginTop: '12px' } }, P.vcKv.el)); c6.add(P.vcOn, P.vcOff);
      host.appendChild(el('div.grid', { style: { marginTop: '16px' } }, c3.el, c4.el, el('div.col.s3', c5.el, c6.el)));
    },
    update(F) {
      const t = F.cur, P = this.p, m = F.meta; if (!t) return;
      const g = t.gpu || {}, c = g.c || {}, rec = D.recNow, base = (m.map || {})[m.base_model], mode = (t.sys && t.sys.mode) || m.mode;
      P.scope.draw(F);
      const sc = F.scope;
      D.txt(P.scSub, mode === 'baseline' ? 'Baseline · MIG 고정 분할' : '최근 ' + (sc && sc.P ? sc.P.length : 80) + ' 슬롯 (0.5 ms/슬롯) · 아래 = SM 0, 위 = SM ' + (m.gpu_sm || 132));
      D.txt(P.scR, sc && sc.last && sc.last.model ? '최근 실행 ' + sc.last.model + ' @ ' + sc.last.sm + ' SM' : mode === 'baseline' ? 'YOLO ' + ((m.baseline || {}).yolo_sm || 60) + ' SM 고정' : 'YOLO 대기');
      P.req.kpi(c.req, g.req, 1, 'req/s', ['VA ', ['b', f(g.va_req, 1)], num(g.vcam_req) ? ' · 가상 카메라 ' : null, num(g.vcam_req) ? ['b', f(g.vcam_req, 1)] : null]);
      verdictTag(P.vtag, g.verdict);
      D.txt(P.busy, fu(g.busy, 1, '%')); D.txt(P.busyS, rec ? '측정 평균 ' + fu(c.busy, 1, '%') : '');
      P.fill.style.width = Math.max(0, Math.min(100, g.busy || 0)) + '%'; P.fill.style.background = g.verdict === 'sat' ? 'var(--bad)' : g.verdict === 'warn' ? 'var(--warn)' : 'var(--blue)';
      P.kv.set('cap', num(g.cap) ? f(g.cap, 1) + ' req/s' : '–', num(g.cap) && g.req > 0.5 ? '지금의 ' + f(g.cap / g.req, 1) + '배' : '');
      P.kv.set('queue', f(g.queue, 0) + ' · ' + (rec ? f(c.queue, 2) : '–')); P.kv.set('wait', fu(g.wait, 1, 'ms'), rec ? '평균 ' + f(c.wait, 1) : '');
      P.kv.set('inf', fu(g.inf, 1, 'ms')); P.kv.set('rej', rec ? f(c.n_rej, 0) + ' / ' + f(c.n_req, 0) : f(g.rej, 0), rec ? '누적' : '최근 1 초');
      P.kv.set('sat', rec ? f(c.sat_s, 0) + ' s · ' + f(c.warn_s, 0) + ' s' : '–', rec ? '/ ' + D.mmss(t.el) : '');
      P.kv.set('sm', f(g.yolo_now, 0) + ' · ' + f(g.rep_sm, 1) + ' SM'); const nv = g.nvml || {};
      P.kv.set('nvml', f(nv.util, 0) + ' % · ' + f(nv.power, 0) + ' W', num(nv.temp) ? f(nv.temp, 0) + ' °C' : '');
      D.txt(P.capNote, '부하율 = ' + (g.busy_src === 'slots' ? 'YOLO가 실행 중이던 슬롯의 비율 (슬롯 CSV)' : '요청률 × 요청당 GPU 추론 시간 (추정)') + '. 감당 가능한 요청률 = 요청률 ÷ 부하율. 판정: 부하율 90 % 이상 · 요청 거절 · 대기열 3 이상이면 포화, 70 % 이상이면 주의.');
      P.c3.draw(F);
      // model x SM: by request while measuring, by slot from the live window otherwise
      let counts = {}, unit = '요청', sub;
      if (rec) {
        if (D.sum(c.combo_req) > 0) { counts = c.combo_req; sub = '측정 시작 후 · 요청 수 기준'; } else { counts = c.combo || {}; unit = '슬롯'; sub = '측정 시작 후 · 실행 슬롯 수 기준'; }
      } else {
        unit = '슬롯'; sub = '최근 60 초 · 실행 슬롯 수 기준';
        for (let i = Math.max(0, F.n - 60); i < F.n; i++) { const cb = F.ticks[i].gpu && F.ticks[i].gpu.combo; for (const k in (cb || {})) counts[k] = (counts[k] || 0) + cb[k]; }
      }
      const tot = P.hm.set(m.models || [], mode === 'baseline' ? [] : (m.classes || []), counts, unit); D.txt(P.hmSub, sub);
      let best = null; for (const k in counts) if (!best || counts[k] > counts[best]) best = k;
      D.fill(P.hmNote, tot ? ['가장 많이 쓰인 조합 ', ['b', best.replace('@', ' @ ') + ' SM'], ' (' + f(100 * counts[best] / tot, 1) + ' %) · 칸 = 전체 중 비율, 진할수록 많음'] : [mode === 'baseline' && !rec ? '측정을 시작하면 요청 기준으로 집계합니다.' : '아직 실행된 요청이 없습니다.']);
      const ret = v => num(v) && base ? 100 * v / base : null, mm = pk(g, 'map');
      P.acc.kpi(ret(c.map), ret(g.map), 1, '%');
      P.akv.set('mean', f(mm, 2)); P.akv.set('base', f(base, 1)); P.akv.set('loss', num(mm) && base ? f(100 * (1 - mm / base), 2) + ' %' : '–', num(mm) && base ? '−' + f(base - mm, 2) + ' mAP' : '');
      const vc = t.vcam; P.vcOn.hidden = !vc; P.vcOff.hidden = !!vc; D.txt(P.vcTag, vc ? '● 켜짐' : '○ 꺼짐'); D.cls(P.vcTag, 'tag' + (vc ? ' good' : ''));
      if (vc) {
        P.vcChips.set('cams', vc.cams + '대'); P.vcChips.set('fps', f(vc.fps, 0)); P.vcChips.set('model', vc.model); P.vcChips.set('q', vc.jpeg_quality); P.vcChips.set('dl', fu(vc.deadline_ms, 0, 'ms')); P.vcChips.set('inf', vc.inflight);
        const rt = vc.rt || []; P.vcKv.set('req', fu(vc.req_s, 1, 'req/s')); P.vcKv.set('met', fu(vc.met_pct, 1, '%')); P.vcKv.set('rt', f(rt[0], 0) + ' · ' + f(rt[1], 0) + ' · ' + f(rt[2], 0) + ' ms');
      }
    },
    scope(F) { this.p.scope.draw(F); },
  };

  // ================================================================== 무선 · 슬라이싱
  V.radio = {
    build(host, meta) {
      const P = this.p = {};
      const c1 = D.card('s6', 'PRB 슬라이싱', ''); P.qSub = c1.subEl; P.qTag = el('span.tag'); c1.right.appendChild(P.qTag);
      P.s1 = sliceBar(); P.s2 = sliceBar(); P.avgRow = el('div', el('div.rowl', el('span', '측정 평균'), el('span.r', '측정 시작 후 누적')), P.s2.el); P.nowR = el('span.r');
      const lg = el('div.legend', { style: { marginTop: '14px' } }); P.lg = {};
      for (const u of D.UE) { P.lg[u.k] = D.legendItem(u.color, u.name, 'box'); lg.appendChild(P.lg[u.k].el); }
      P.srcNote = el('div.note');
      c1.add(el('div.rowl', { style: { marginTop: '0' } }, el('span', '지금'), P.nowR), P.s1.el, P.avgRow, lg, P.srcNote);
      const q = (label, color, svc) => ({ label, color, dash: true, plain: true, inst: t => t.svc[svc] ? t.svc[svc].quota : null });
      const c2 = chartCard('s6', '서비스별 PRB 할당과 사용', '슬롯당 PRB', { unit: 'PRB', series: [ser('VA 사용', '--c1', 'svc.va', 'prb'), q('VA 할당', '--c1', 'va'), ser('Non-VA 사용', '--c3', 'svc.nv', 'prb'), q('Non-VA 할당', '--c3', 'nv')] }, 250);
      P.c2 = c2.chart;
      host.appendChild(el('div.grid', c1.el, c2.el));
      const all = key => D.UE.map(u => ser(u.name, u.color, 'ue.' + u.k, key));
      const a = chartCard('s4', 'UE별 PRB 사용', '슬롯당 평균', { unit: 'PRB', series: all('prb') }, 220);
      const b = chartCard('s4', 'UE별 Throughput', 'Uplink MAC', { unit: 'Mbps', series: all('mac') }, 220);
      const c = chartCard('s4', '서비스 · 셀 Throughput', 'Uplink MAC', { unit: 'Mbps', series: [ser('VA 서비스', '--c1', 'svc.va', 'mac'), ser('Non-VA 서비스', '--c3', 'svc.nv', 'mac'), ser('셀 합계', '--total', 'cell', 'mac', { heavy: true })] }, 220);
      host.appendChild(el('div.grid', { style: { marginTop: '16px' } }, a.el, b.el, c.el));
      // the UE table (also the table twin of the charts)
      const cols = ['UE', 'RNTI', 'IMSI', 'IP', 'SNR (dB)', 'UL MCS', 'UL BLER (%)', 'DL MCS', 'DL BLER (%)', 'RSRP (dBm)', 'PH (dB)', 'PRB', 'UL MAC (Mbps)', 'UL 버퍼 (KB)', 'PRB 출처'];
      const tb = el('tbody'); P.rows = {};
      for (const u of D.UE) {
        const tds = cols.map((x, i) => el('td' + (i === 0 ? '' : ''))), nm = el('span.nm', el('i', { style: { background: cvar(u.color) } }), u.name);
        tds[0].appendChild(nm); P.rows[u.k] = tds; tb.appendChild(el('tr', tds));
      }
      const c3 = D.card('s12', 'UE 무선 상태', ''); P.tSub = c3.subEl;
      c3.add(el('div', { style: { overflowX: 'auto' } }, el('table.tbl', el('thead', el('tr', cols.map(x => el('th', x)))), tb)));
      host.appendChild(el('div.grid', { style: { marginTop: '16px' } }, c3.el));
      const d = chartCard('s4', 'SNR', 'UE별 · PUSCH', { unit: 'dB', series: all('snr') }, 200);
      const e = chartCard('s4', 'UL BLER', 'UE별 · gNB 보고값', { unit: '%', digits: 1, series: D.UE.map(u => { const s = ser(u.name, u.color, 'ue.' + u.k, 'bler'), i = s.inst, cu = s.cum; s.inst = t => pctf(i(t)); s.cum = t => pctf(cu(t)); return s; }) }, 200);
      const g = chartCard('s4', 'UL MCS', 'UE별', { unit: '', digits: 1, series: all('mcs') }, 200);
      P.charts = [a.chart, b.chart, c.chart, d.chart, e.chart, g.chart];
      host.appendChild(el('div.grid', { style: { marginTop: '16px' } }, d.el, e.el, g.el));
    },
    update(F) {
      const t = F.cur, P = this.p, m = F.meta; if (!t) return;
      const rec = D.recNow, va = t.svc.va || {}, nv = t.svc.nv || {}, noq = !num(va.quota) && !num(nv.quota), cell = t.cell || {};
      D.txt(P.qSub, 'Uplink ' + (m.prb_total || 273) + ' PRB · 서비스 블록 = 할당 (quota), 진한 색 = UE가 실제로 쓴 PRB');
      D.txt(P.qTag, noq ? '슬라이싱 없음 (PF 공유)' : '슬라이싱 적용 중'); D.cls(P.qTag, 'tag' + (noq ? '' : ' blue'));
      D.txt(P.nowR, '셀 사용 ' + f(cell.prb, 1) + ' · 미사용 ' + f(cell.unused, 1) + ' PRB');
      P.s1.set(sliceData(t, m, false)); P.avgRow.hidden = !rec; if (rec) P.s2.set(sliceData(t, m, true));
      const srcs = {};
      for (const u of D.UE) {
        const x = t.ue[u.k] || {}, xc = x.c || {}, r = P.rows[u.k], mu = (m.ues || {})[u.k] || {};
        P.lg[u.k].set(f(x.prb, 1) + (rec ? ' · 평균 ' + f(xc.prb, 1) : ''), 'PRB'); if (x.prbsrc) srcs[x.prbsrc] = 1;
        const cell2 = (i, now, avg, d) => D.fill(r[i], rec ? [f(now, d), ['small', '평균 ' + f(avg, d)]] : [f(now, d)]);
        D.txt(r[1], x.rnti || mu.rnti || '–'); D.txt(r[2], mu.imsi || '–'); D.txt(r[3], mu.ip || '–');
        cell2(4, x.snr, xc.snr, 1); cell2(5, x.mcs, xc.mcs, 1); cell2(6, pctf(x.bler), pctf(num(xc.blerx) ? xc.blerx : xc.bler), 2); cell2(7, x.dlmcs, xc.dlmcs, 1); cell2(8, pctf(x.dlbler), pctf(xc.dlbler), 2);
        cell2(9, x.rsrp, xc.rsrp, 0); cell2(10, x.ph, xc.ph, 0); cell2(11, x.prb, xc.prb, 1); cell2(12, x.mac, xc.mac, 2);
        cell2(13, num(x.buf) ? x.buf / 1000 : null, num(xc.buf) ? xc.buf / 1000 : null, 0); D.txt(r[14], PSRC[x.prbsrc] || '–');
      }
      D.txt(P.srcNote, '할당 출처: ' + (QSRC[t.quota_src] || '없음') + ' · UE별 PRB 출처: ' + (Object.keys(srcs).map(k => PSRC[k] || k).join(', ') || '없음') + '. 같은 서비스 안의 UE 간 배분은 gNB MAC 스케줄러 (PF)가 정합니다.');
      D.txt(P.tSub, rec ? '지금 값 · 작은 글씨 = 측정 시작 후 평균 (UL BLER 평균 = 재전송 수 ÷ 첫 전송 수)' : '지금 값 (gNB 1.28 초 통계)');
      P.c2.draw(F); for (const ch of P.charts) ch.draw(F);
    },
  };

  // ================================================================== 결과 요약 (the summary of one measurement)
  D.summaryView = function (s, id) {
    const va = (s.svc || {}).va || {}, nv = (s.svc || {}).nv || {}, g = s.gpu || {}, cell = s.cell || {}, acc = s.accuracy || {};
    const T = (label, v, d, unit, sub, cls) => { const t = D.tile(label, cls || 'in'); D.noAnim = true; t.set(v, d, unit); D.noAnim = false; D.fill(t.sub, sub || []); return t.el; };
    const out = el('div');
    out.appendChild(el('div.chips', { style: { marginBottom: '14px' } },
      el('span.chip', '시작', el('b', s.start || '–')), el('span.chip', '종료', el('b', s.end || '–')), el('span.chip', '길이', el('b', D.mmss(s.duration_s))),
      el('span.chip', '모드', el('b', s.mode === 'baseline' ? 'Baseline (MIG)' : 'Ours')), s.sim ? el('span.tag.warn', '▲ 시뮬레이션 데이터') : null));
    out.appendChild(el('div.sumgrid.c6',
      T('VA QoS 충족률', va.qos, 1, '%', ['기한 ', ['b', f(s.deadline_ms, 0) + ' ms'], ' · ', ['b', f(va.n, 0)], ' 프레임']),
      T('VA E2E Latency', va.e2e, 1, 'ms', ['UL ', ['b', f(va.ul, 1)], ' + Inference ', ['b', f(va.ai, 1)]]),
      T('셀 Uplink Throughput', cell.mac, 1, 'Mbps', ['VA ', ['b', f(va.mac, 1)], ' · Non-VA ', ['b', f(nv.mac, 1)]]),
      T('GPU YOLO 요청', g.req, 1, 'req/s', ['부하율 ', ['b', fu(g.busy, 0, '%')], ' · 거절 ', ['b', f(g.n_rej, 0)]]),
      T('mAP 유지율', acc.retention_pct, 1, '%', ['평균 mAP ', ['b', f(acc.map_mean, 2)], ' / ' + (s.base_model || '26x') + ' ' + f(acc.map_base, 1)]),
      T('Non-VA QoS 충족률', nv.qos, 1, '%', ['기한 ', ['b', f(s.nv_deadline_ms, 0) + ' ms'], ' · UL ', ['b', f(nv.ul, 1)], ' ms · ', ['b', f(nv.n, 0)], ' 프레임'])));
    const cols = ['UE', 'RNTI', '프레임', 'FPS', 'UL (ms)', 'UL p90 / p99', 'Inference (ms)', 'E2E (ms)', 'E2E p90 / p99', 'QoS (%)', '앱 Mbps', 'MAC Mbps', 'PRB', 'SNR (dB)', 'MCS', 'BLER (%)'];
    const tb = el('tbody');
    for (const u of D.UE) {
      const x = (s.ue || {})[u.k] || {}, isva = u.svc === 'va', pp = a => a && num(a[1]) ? f(a[1], 0) + ' / ' + f(a[2], 0) : '–';
      const vals = [null, x.rnti || '–', f(x.n, 0), f(x.fps, 1), f(x.ul, 1), pp(x.ulp), isva ? f(x.ai, 1) : '–', isva ? f(x.e2e, 1) : '–', isva ? pp(x.e2ep) : '–', f(x.qos, 1), f(x.mbps, 2), f(x.mac, 2), f(x.prb, 1), f(x.snr, 1), f(x.mcs, 1), f(pctf(num(x.blerx) ? x.blerx : x.bler), 2)];
      const tr = el('tr', vals.map((v, i) => i ? el('td', v) : el('td', el('span.nm', el('i', { style: { background: cvar(u.color) } }), u.name))));
      tb.appendChild(tr);
    }
    out.appendChild(el('div', { style: { overflowX: 'auto', margin: '18px 0 6px' } }, el('table.tbl', el('thead', el('tr', cols.map(x => el('th', x)))), tb)));
    const kv = (title, rows) => { const k = D.kv(rows.map((r, i) => [r[0], 'k' + i]), 'big'); rows.forEach((r, i) => k.set('k' + i, r[1], r[2])); return el('div.uec', el('div', { style: { fontWeight: 700, fontSize: '14px' } }, title), k.el); };
    const sm = g.sm || {}, combo = D.sum(g.combo_req) > 0 ? g.combo_req : (g.combo || {}), ctot = D.sum(combo);
    const top = Object.keys(combo).sort((a, b) => combo[b] - combo[a]).slice(0, 4).map(k => k.replace('@', ' @ ') + ' SM ' + f(100 * combo[k] / ctot, 0) + ' %').join(' · ');
    out.appendChild(el('div.sumgrid', { style: { gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', marginTop: '12px' } },
      kv('PRB 슬라이싱 (평균)', [['VA 할당 · 사용', f(va.quota, 0) + ' · ' + f(va.prb, 1), 'PRB'], ['Non-VA 할당 · 사용', f(nv.quota, 0) + ' · ' + f(nv.prb, 1), 'PRB'], ['셀 사용 · 미사용', f(cell.prb, 1) + ' · ' + f(cell.unused, 1), 'PRB'], ['할당 출처', QSRC[s.quota_src] || '슬라이싱 없음']]),
      kv('GPU · YOLO', [['요청 (누적 · 거절)', f(g.n_req, 0) + ' · ' + f(g.n_rej, 0)], ['감당 가능한 요청률', fu(g.cap, 1, 'req/s')], ['대기열 · 대기 시간', f(g.queue, 2) + ' · ' + fu(g.wait, 1, 'ms')], ['포화 · 주의 시간', f(g.sat_s, 0) + ' s · ' + f(g.warn_s, 0) + ' s']]),
      kv('SM · 모델', [['슬롯 평균 SM (RAN · YOLO 실행)', f(sm.ran, 1) + ' · ' + f(sm.run, 1)], ['YOLO에 제공 · 사용 불가', f(sm.free, 1) + ' · ' + f(sm.unus, 1), 'SM'], ['모델 × SM', top || '–'], ['정확도 감소율', fu(acc.loss_pct, 2, '%')]])));
    if (s.notes && s.notes.length) out.appendChild(el('div.note', s.notes.map(n => el('div', '· ' + n))));
    if (id && s.files && s.files.length) out.appendChild(el('div', { style: { marginTop: '14px' } }, el('div.rowl', el('span', '저장된 파일'), el('span.r', s.dir || '')),
      el('div.files', s.files.map(fn => el('a', { href: '/api/recs/' + encodeURIComponent(id) + '/file/' + encodeURIComponent(fn), download: fn }, fn)))));
    return out;
  };

  // ================================================================== 기록 · Replay
  V.rec = {
    build(host, meta) {
      const P = this.p = {};
      const c1 = D.card('s8', '저장된 측정', ''); P.sub = c1.subEl; P.body = el('tbody');
      const rf = el('button.btn.ghost.sm', { onclick: () => this.refresh() }, '새로 고침'); c1.right.appendChild(rf);
      c1.add(el('div', { style: { overflowX: 'auto' } }, el('table.tbl', el('thead', el('tr', ['이름', '시작', '길이', '모드', 'QoS', 'E2E', 'GPU 요청', '', ''].map((x, i) => el('th' + (i === 7 ? '.l' : ''), x)))), P.body)));
      P.empty = el('div.empty', '아직 저장된 측정이 없습니다. 위의 “측정 시작”으로 기록하세요.'); c1.add(P.empty);
      const c2 = D.card('s4', 'Replay 파일 열기', '다른 PC에서 받은 기록도 재생');
      const inp = el('input', { type: 'file', accept: '.jsonl,.json', style: { display: 'none' }, onchange: e => { if (e.target.files[0]) D.act.replayFile(e.target.files[0]); e.target.value = ''; } });
      const dz = el('div.drop', el('div', el('b', '.replay.jsonl'), ' 파일을 여기에 끌어다 놓거나'), el('div', { style: { marginTop: '10px' } }, el('button.btn.ghost.sm', { onclick: () => inp.click() }, '파일 선택')), inp);
      dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('over'); }); dz.addEventListener('dragleave', () => dz.classList.remove('over'));
      dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('over'); const fl = e.dataTransfer.files[0]; if (fl) D.act.replayFile(fl); });
      c2.add(dz, el('div.note', '측정마다 저장되는 파일: ', el('b', '.replay.jsonl'), ' (이 화면에서 다시 재생) · ', el('b', '.summary.json / .txt'), ' (결과 요약) · ', el('b', '.timeseries.csv'), ' (1 초 단위 전체 지표) · ', el('b', '.frames.csv'), ' (프레임별 Latency) · ', el('b', '.slots.csv.gz'), ' (슬롯별 SM)'));
      host.appendChild(el('div.grid', c1.el, c2.el));
      P.sumCard = D.card('s12', '결과 요약', ''); P.sumHost = el('div'); P.sumCard.add(P.sumHost); P.sumGrid = el('div.grid', { style: { marginTop: '16px' } }, P.sumCard.el); P.sumGrid.hidden = true;
      host.appendChild(P.sumGrid);
      this.refresh();
    },
    showSummary(s, id, name) { const P = this.p; if (!P) return; P.sumGrid.hidden = !s; if (s) { D.txt(P.sumCard.subEl || el('span'), ''); P.sumCard.head.querySelector('.t').textContent = '결과 요약 · ' + (name || s.name || ''); P.sumHost.textContent = ''; P.sumHost.appendChild(D.summaryView(s, id)); } },
    async refresh() {
      const P = this.p; if (!P) return;
      let d; try { d = await D.api('/api/recs'); } catch (e) { D.txt(P.sub, '목록을 읽지 못했습니다: ' + e.message); return; }
      D.txt(P.sub, d.dir + ' · 디스크 여유 ' + f(d.free_gb, 0) + ' GB'); P.body.textContent = ''; P.empty.hidden = d.recs.length > 0;
      for (const r of d.recs) {
        const rp = (r.files || []).find(x => /\.replay\.jsonl$/.test(x.name));
        const st = r.running ? el('span.tag.bad', '● 기록 중') : r.unfinished ? el('span.tag.warn', '▲ 비정상 종료') : r.broken ? el('span.tag.warn', '▲ 요약 손상') : null;
        const files = el('div.files', (r.files || []).map(x => el('a', { href: '/api/recs/' + encodeURIComponent(r.id) + '/file/' + encodeURIComponent(x.name), download: x.name, title: D.bytes(x.bytes), onclick: e => e.stopPropagation() }, x.name.replace(/^.*?\.(replay|summary|timeseries|frames|slots)\./, '$1.'))));
        const btns = el('div', { style: { display: 'flex', gap: '6px', justifyContent: 'flex-end' } },
          r.summary !== false && !r.running && !r.unfinished && !r.broken ? el('button.btn.ghost.sm', { onclick: e => { e.stopPropagation(); D.act.openSummary(r.id, r.name); } }, '요약') : null,
          rp ? el('button.btn.primary.sm', { onclick: e => { e.stopPropagation(); D.act.replayServer(r.id, r.name || r.id); } }, '▶ Replay') : null);
        P.body.appendChild(el('tr', el('td', el('span.nm', r.name || r.id), st ? ' ' : null, st, r.sim ? el('small', '시뮬레이션') : null), el('td', r.start || '–'), el('td', num(r.duration_s) ? D.mmss(r.duration_s) : '–'),
          el('td', r.mode === 'baseline' ? 'Baseline' : r.mode === 'ours' ? 'Ours' : '–'), el('td', fu(r.qos, 1, '%')), el('td', fu(r.e2e, 1, 'ms')), el('td', fu(r.req, 1, 'req/s')), el('td.l', files), el('td', btns)));
      }
    },
    update() { },
  };
})();
