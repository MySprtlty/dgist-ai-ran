#!/usr/bin/env python3
"""model_select.py - rule-based model selection for the RUN requests sent to the dApp agent (2026-11-13).

.env   VA_MODEL_SELECT=1                  0 = every request asks for VA_MODEL (as before)
       VA_MODEL_WEIGHTS=10,10,10,10,60    requests per block for 26n,26s,26m,26l,26x (in this order)

A block is sum(weights) consecutive requests (100 here) and holds exactly weights[i] requests of model i in a random order: the
selector fills a bag with weights[i] entries per model, shuffles it, hands out one entry per request and refills the bag when it
is empty. "10,10,10,10,60" therefore means: of every 100 requests exactly 10 run 26n, 10 26s, 10 26m, 10 26l and 60 26x, and
which request gets which model is random. One sequence per process (VA service: all its UEs and the background UE together).

A model the agent does not offer cannot run (its welcome message lists the loaded models; the Baseline YOLO server loads only
those with a MIG engine): such a draw is sent as the fallback model (VA_MODEL) and counted as "replaced".

VA_MODEL_SEED=<int> fixes the order (repeatable runs); unset = a different order at every start.

    python3 model_select.py [weights] [requests]     # print the first requests of a sequence and the per-model counts
"""
import os
import random
import threading

MODELS = ("26n", "26s", "26m", "26l", "26x")
MAX_BLOCK = 100000


def parse_weights(text):
    """'10,10,10,10,60' -> (10, 10, 10, 10, 60); ValueError carrying the reason otherwise"""
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != len(MODELS):
        raise ValueError(f"{len(MODELS)} comma-separated counts expected (order {','.join(MODELS)}), got {len(parts)}")
    try:
        w = tuple(int(p) for p in parts)
    except ValueError:
        raise ValueError("the counts must be whole numbers") from None
    if min(w) < 0:
        raise ValueError("a count must not be negative")
    if not 0 < sum(w) <= MAX_BLOCK:
        raise ValueError(f"the counts must add up to 1..{MAX_BLOCK}")
    return w


class ModelSelector:
    def __init__(self, weights, fallback="26x", seed=None):
        self.weights, self.fallback, self.block = tuple(weights), fallback, sum(weights)
        self._rng = random.Random(seed)
        self._lock = threading.Lock()               # pick() runs in the VA service's thread pool
        self._bag = []
        self.available = None                       # models the agent offers; None = not known (no welcome yet): nothing is replaced
        self.drawn = dict.fromkeys(MODELS, 0)       # what the rule chose
        self.sent = dict.fromkeys(MODELS, 0)        # what was asked for (after the replacement of models the agent lacks)
        self.replaced = 0

    def _missing(self):
        return [] if self.available is None else [m for m, w in zip(MODELS, self.weights) if w and m not in self.available]

    def set_available(self, names):
        """the models of the agent's welcome message; returns the models of the rule that it lacks"""
        with self._lock:
            self.available = tuple(names) if names else None
            return self._missing()

    def pick(self):
        """the model of the next request"""
        with self._lock:
            if not self._bag:
                self._bag = [m for m, w in zip(MODELS, self.weights) for _ in range(w)]
                self._rng.shuffle(self._bag)
            m = self._bag.pop()
            self.drawn[m] += 1
            if self.available is not None and m not in self.available:
                m = self.fallback
                self.replaced += 1
            self.sent[m] = self.sent.get(m, 0) + 1
            return m

    def label(self):
        return f"{'/'.join(MODELS)} = {':'.join(str(w) for w in self.weights)}"

    def snapshot(self):
        with self._lock:
            return {"weights": dict(zip(MODELS, self.weights)), "block": self.block, "fallback": self.fallback, "missing": self._missing(),
                    "requests": sum(self.drawn.values()), "drawn": dict(self.drawn), "sent": dict(self.sent), "replaced": self.replaced}


def enabled(env=None):
    env = os.environ if env is None else env
    return str(env.get("VA_MODEL_SELECT", "0")).strip().lower() in ("1", "on", "true", "yes")


def from_env(fallback="26x", env=None, log=print):
    """the selector the environment asks for, or None = fixed model (switch off, or weights unusable: one line says why)"""
    env = os.environ if env is None else env
    if not enabled(env):
        return None
    try:
        if not str(env.get("VA_MODEL_WEIGHTS", "")).strip():
            raise ValueError("VA_MODEL_WEIGHTS is not set")
        weights = parse_weights(env["VA_MODEL_WEIGHTS"])
        seed = int(env["VA_MODEL_SEED"]) if str(env.get("VA_MODEL_SEED", "")).strip() else None
    except ValueError as exc:
        log(f"!! VA_MODEL_SELECT=1 ignored, every request uses {fallback}: VA_MODEL_WEIGHTS={env.get('VA_MODEL_WEIGHTS')!r} "
            f"VA_MODEL_SEED={env.get('VA_MODEL_SEED')!r} - {exc}")
        return None
    return ModelSelector(weights, fallback=fallback, seed=seed)


if __name__ == "__main__":
    import sys
    sel = ModelSelector(parse_weights(sys.argv[1] if len(sys.argv) > 1 else "10,10,10,10,60"))
    n = int(sys.argv[2]) if len(sys.argv) > 2 else sel.block
    seq = [sel.pick() for _ in range(n)]
    print(f"{sel.label()} (block {sel.block}), first {min(n, 40)} of {n} requests: {' '.join(m[2] for m in seq[:40])}")
    print("counts:", {m: c for m, c in sel.snapshot()["drawn"].items()})
