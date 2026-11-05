// Client-side E2E is measured before posting telemetry; this POST is not E2E.
class TelemetryClient {
    constructor(ue, token = null) {
        this.ue = ue; this.token = token; this.timer = null; this.busy = false;
        this.pending = new Set();
    }
    async call(action, data = {}) {
        const abort = new AbortController();
        const timer = setTimeout(() => abort.abort(), 2000);
        try {
            const r = await fetch('/telemetry/' + action, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ue_id: this.ue, session_token: this.token, ...data}),
                signal: abort.signal
            });
            if (!r.ok) throw new Error(await r.text());
            return await r.json();
        } finally { clearTimeout(timer); }
    }
    async start() {
        if (!this.token) this.token = (await this.call('start')).session_token;
        this.timer = setInterval(async () => {
            if (this.busy) return;
            this.busy = true;
            try { await this.call('heartbeat'); }
            catch (e) { console.error('PPO telemetry heartbeat:', e); }
            finally { this.busy = false; }
        }, 500);
        return this;
    }
    report(data) {
        const p = this.call('result', data).catch(e => console.error('PPO latency report:', e));
        this.pending.add(p);
        p.finally(() => this.pending.delete(p));
        return p;
    }
    result(frame, latency, bytes, sampleMs) {
        return this.report({frame_id: frame, latency_ms: latency,
            payload_bytes: bytes, sample_ms: sampleMs, success: true});
    }
    failure(frame) {
        return this.report({frame_id: frame, success: false});
    }
    close() { clearInterval(this.timer); this.timer = null; }
    async stop() {
        await Promise.all([...this.pending]);
        await this.call('stop');
        this.close();
    }
}

