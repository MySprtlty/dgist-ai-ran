/*
 * ra_slicing_link.h (ICNL, 2026-11-11) -- header-only C client for the link between the Network Slicing xApp (PPO) and
 * the Resource Allocator (RA) of the IITP 6G demo. Protocol: INTERFACE.md next to this file (v1).
 *
 *   UDP/IPv4, one datagram = one JSON object. Slicing -> RA: "budget", "bye". RA -> Slicing: "ra_state" (10 ms), "ra_qos" (100 ms).
 *
 *   rsl_link_t l;
 *   rsl_open(&l, "127.0.0.1:9821", "127.0.0.1:9820");               // listen here, RA there
 *   rsl_send_budget(&l, run, ++seq, 150, 123, 273, 5, va, 2, nonva, 2);
 *   rsl_state_t st; rsl_qos_t q;
 *   int t = rsl_recv(&l, 10, &st, &q);                              // RSL_STATE / RSL_QOS / RSL_NONE / RSL_OTHER, < 0 on error
 *   rsl_close(&l);
 *
 * C11 + POSIX sockets, no other dependency. The JSON reader handles the flat messages of this protocol (objects, arrays of
 * objects, numbers, strings, null); keys are matched at the right nesting level. It is not a general JSON library.
 */
#ifndef RA_SLICING_LINK_H
#define RA_SLICING_LINK_H

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifndef MSG_DONTWAIT
#define MSG_DONTWAIT 0   /* hidden by strict POSIX on some systems (macOS); reads follow poll(), UDP sends do not block */
#endif

#define RSL_VERSION 1
#define RSL_MAX_UES 16
#define RSL_MAX_DGRAM 1400
#define RSL_DEFAULT_RA "127.0.0.1:9820"
#define RSL_DEFAULT_SLICING "127.0.0.1:9821"

enum { RSL_NONE = 0, RSL_STATE = 1, RSL_QOS = 2, RSL_OTHER = 3 };

typedef struct {
  uint16_t rnti;
  int svc;                      /* 0 = not associated, 1 = VA, 2 = Non-VA */
} rsl_ue_t;

typedef struct {
  long long t_ms;               /* sender CLOCK_REALTIME ms */
  unsigned long long seq;       /* ra_state counter */
  unsigned long long run, budget_seq; /* last budget the RA processed (0 = none) */
  char status[16];              /* none | pending | applied | rejected */
  char reason[96];
  int has_applied;
  unsigned applied_va, applied_nonva; /* quotas in force in the MAC */
  int n_ues;
  rsl_ue_t ues[RSL_MAX_UES];
} rsl_state_t;

typedef struct {
  int requests, on_time;
  double deadline_ms, e2e_p50_ms, net_p50_ms, proc_p50_ms; /* < 0 = null */
} rsl_svc_qos_t;

typedef struct {
  long long t_ms;
  int window_ms;
  rsl_svc_qos_t va, nonva;
  char raw[RSL_MAX_DGRAM + 1];  /* the whole datagram (per-UE part is left to the caller) */
} rsl_qos_t;

typedef struct {
  int fd;
  struct sockaddr_in peer;
} rsl_link_t;

/* ---------------------------------------------------------------------------------------------------- helpers */
static inline long long rsl_wall_ms(void)
{
  struct timespec t;
  clock_gettime(CLOCK_REALTIME, &t);
  return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static inline int rsl_parse_hostport(const char* s, struct sockaddr_in* a)
{
  char host[64];
  const char* col = s ? strrchr(s, ':') : NULL;
  if (!col || col == s || (size_t)(col - s) >= sizeof(host)) return -1;
  memcpy(host, s, (size_t)(col - s));
  host[col - s] = 0;
  char* end = NULL;
  long port = strtol(col + 1, &end, 10);
  if (!end || *end || port <= 0 || port > 65535) return -1;
  memset(a, 0, sizeof(*a));
  a->sin_family = AF_INET;
  a->sin_port = htons((uint16_t)port);
  return inet_pton(AF_INET, host, &a->sin_addr) == 1 ? 0 : -1;
}

/* JSON: s points at '{' or '['; returns the first character after the matching bracket, or NULL */
static inline const char* rsl__skip(const char* p, const char* e)
{
  if (p >= e) return NULL;
  if (*p == '"') {
    for (p++; p < e; p++) {
      if (*p == '\\') { p++; continue; }
      if (*p == '"') return p + 1;
    }
    return NULL;
  }
  if (*p == '{' || *p == '[') {
    int depth = 0;
    for (; p < e; p++) {
      if (*p == '"') { p = rsl__skip(p, e); if (!p) return NULL; p--; continue; }
      if (*p == '{' || *p == '[') depth++;
      else if (*p == '}' || *p == ']') { if (--depth == 0) return p + 1; }
    }
    return NULL;
  }
  while (p < e && *p != ',' && *p != '}' && *p != ']') p++;   /* number, true, false, null */
  return p;
}

/* the value of `key` at the top level of the object that starts at obj ('{'); NULL if absent */
static inline const char* rsl__get(const char* obj, const char* e, const char* key)
{
  if (!obj || obj >= e || *obj != '{') return NULL;
  const size_t kl = strlen(key);
  const char* p = obj + 1;
  while (p < e) {
    while (p < e && (isspace((unsigned char)*p) || *p == ',')) p++;
    if (p >= e || *p == '}') return NULL;
    if (*p != '"') return NULL;
    const char* kend = rsl__skip(p, e);
    if (!kend) return NULL;
    const int match = (size_t)(kend - p - 2) == kl && !memcmp(p + 1, key, kl);
    p = kend;
    while (p < e && isspace((unsigned char)*p)) p++;
    if (p >= e || *p != ':') return NULL;
    p++;
    while (p < e && isspace((unsigned char)*p)) p++;
    if (match) return p;
    p = rsl__skip(p, e);
    if (!p) return NULL;
  }
  return NULL;
}

static inline double rsl__num(const char* obj, const char* e, const char* key, double def)
{
  const char* v = rsl__get(obj, e, key);
  if (!v || *v == 'n') return def;          /* absent or null */
  char* end = NULL;
  double d = strtod(v, &end);
  return end && end != v ? d : def;
}

static inline unsigned long long rsl__u64(const char* obj, const char* e, const char* key, unsigned long long def)
{
  const char* v = rsl__get(obj, e, key);
  if (!v || *v < '0' || *v > '9') return def;
  char* end = NULL;
  unsigned long long x = strtoull(v, &end, 10);
  return end && end != v ? x : def;
}

static inline int rsl__str(const char* obj, const char* e, const char* key, char* out, size_t n)
{
  const char* v = rsl__get(obj, e, key);
  if (n) out[0] = 0;
  if (!v || *v != '"') return 0;
  const char* end = rsl__skip(v, e);
  if (!end) return 0;
  size_t len = (size_t)(end - v - 2);
  if (len >= n) len = n - 1;
  memcpy(out, v + 1, len);
  out[len] = 0;
  return 1;
}

static inline void rsl__svc_qos(const char* obj, const char* e, rsl_svc_qos_t* q)
{
  memset(q, 0, sizeof(*q));
  q->deadline_ms = q->e2e_p50_ms = q->net_p50_ms = q->proc_p50_ms = -1.0;
  if (!obj || *obj != '{') return;
  q->requests = (int)rsl__num(obj, e, "requests", 0);
  q->on_time = (int)rsl__num(obj, e, "on_time", 0);
  q->deadline_ms = rsl__num(obj, e, "deadline_ms", -1.0);
  q->e2e_p50_ms = rsl__num(obj, e, "e2e_p50_ms", -1.0);
  q->net_p50_ms = rsl__num(obj, e, "net_p50_ms", -1.0);
  q->proc_p50_ms = rsl__num(obj, e, "proc_p50_ms", -1.0);
}

/* ------------------------------------------------------------------------------------------------------- API */
/* Bind `listen` (host:port) and remember `peer`. Returns 0 or -1 (errno set). The socket is close-on-exec. */
static inline int rsl_open(rsl_link_t* l, const char* listen, const char* peer)
{
  struct sockaddr_in me;
  l->fd = -1;
  if (rsl_parse_hostport(listen, &me) || rsl_parse_hostport(peer, &l->peer)) { errno = EINVAL; return -1; }
  l->fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (l->fd < 0) return -1;
  (void)fcntl(l->fd, F_SETFD, FD_CLOEXEC);
  int one = 1;
  (void)setsockopt(l->fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  if (bind(l->fd, (struct sockaddr*)&me, sizeof(me)) < 0) { int err = errno; close(l->fd); l->fd = -1; errno = err; return -1; }
  return 0;
}

static inline void rsl_close(rsl_link_t* l)
{
  if (l->fd >= 0) close(l->fd);
  l->fd = -1;
}

static inline int rsl__send(rsl_link_t* l, const char* buf, int len)
{
  if (len <= 0 || len > RSL_MAX_DGRAM) return -1;
  return sendto(l->fd, buf, (size_t)len, MSG_DONTWAIT, (const struct sockaddr*)&l->peer, sizeof(l->peer)) == len ? 0 : -1;
}

static inline int rsl__rntis(char* out, size_t n, const uint16_t* r, int k)
{
  size_t used = 0;
  out[used++] = '[';
  for (int i = 0; i < k && used + 8 < n; i++)
    used += (size_t)snprintf(out + used, n - used, "%s\"%04x\"", i ? "," : "", r[i]);
  if (used + 2 > n) return -1;
  out[used++] = ']';
  out[used] = 0;
  return 0;
}

/* Slicing -> RA: a new service budget (INTERFACE.md 2.1). Returns 0 when the datagram left. */
static inline int rsl_send_budget(rsl_link_t* l, unsigned long long run, unsigned long long seq, unsigned va_prb, unsigned nonva_prb,
                           unsigned total, unsigned min_prb, const uint16_t* va_rntis, int n_va, const uint16_t* nonva_rntis,
                           int n_nonva)
{
  char va[160], nv[160], buf[RSL_MAX_DGRAM + 1];
  if (rsl__rntis(va, sizeof(va), va_rntis, n_va) || rsl__rntis(nv, sizeof(nv), nonva_rntis, n_nonva)) return -1;
  const int len = snprintf(buf, sizeof(buf),
                           "{\"type\":\"budget\",\"v\":%d,\"t_ms\":%lld,\"src\":\"ppo\",\"run\":%llu,\"seq\":%llu,"
                           "\"va_prb\":%u,\"nonva_prb\":%u,\"total\":%u,\"min_prb\":%u,\"va_rntis\":%s,\"nonva_rntis\":%s}",
                           RSL_VERSION, rsl_wall_ms(), run, seq, va_prb, nonva_prb, total, min_prb, va, nv);
  return len < (int)sizeof(buf) ? rsl__send(l, buf, len) : -1;
}

/* Slicing -> RA: leaving (INTERFACE.md 2.2) */
static inline int rsl_send_bye(rsl_link_t* l, unsigned long long run)
{
  char buf[160];
  const int len = snprintf(buf, sizeof(buf), "{\"type\":\"bye\",\"v\":%d,\"t_ms\":%lld,\"src\":\"ppo\",\"run\":%llu}",
                           RSL_VERSION, rsl_wall_ms(), run);
  return rsl__send(l, buf, len);
}

/* Parse one datagram. Returns RSL_STATE (st filled), RSL_QOS (q filled), RSL_OTHER (valid JSON object of another type)
 * or -1 (not a v1 message of this protocol). */
static inline int rsl_parse(const char* buf, size_t n, rsl_state_t* st, rsl_qos_t* q)
{
  const char* e = buf + n;
  const char* o = buf;
  while (o < e && isspace((unsigned char)*o)) o++;
  if (o >= e || *o != '{' || !rsl__skip(o, e)) return -1;
  char type[24];
  if (!rsl__str(o, e, "type", type, sizeof(type)) || (int)rsl__num(o, e, "v", 0) != RSL_VERSION) return -1;
  if (!strcmp(type, "ra_state")) {
    if (!st) return RSL_OTHER;
    memset(st, 0, sizeof(*st));
    st->t_ms = (long long)rsl__num(o, e, "t_ms", 0);
    st->seq = rsl__u64(o, e, "seq", 0);
    st->run = rsl__u64(o, e, "run", 0);
    st->budget_seq = rsl__u64(o, e, "budget_seq", 0);
    if (!rsl__str(o, e, "status", st->status, sizeof(st->status))) return -1;
    rsl__str(o, e, "reason", st->reason, sizeof(st->reason));
    const char* a = rsl__get(o, e, "applied");
    if (a && *a == '{') {
      st->has_applied = 1;
      st->applied_va = (unsigned)rsl__num(a, e, "va", 0);
      st->applied_nonva = (unsigned)rsl__num(a, e, "nonva", 0);
    }
    const char* u = rsl__get(o, e, "ues");
    if (u && *u == '[') {
      const char* end = rsl__skip(u, e);
      const char* p = u + 1;
      while (p && end && p < end && st->n_ues < RSL_MAX_UES) {
        while (p < end && (isspace((unsigned char)*p) || *p == ',')) p++;
        if (p >= end || *p != '{') break;
        char r[8];
        if (rsl__str(p, e, "rnti", r, sizeof(r))) {
          st->ues[st->n_ues].rnti = (uint16_t)strtoul(r, NULL, 16);
          st->ues[st->n_ues].svc = (int)rsl__num(p, e, "svc", 0);
          st->n_ues++;
        }
        p = rsl__skip(p, e);
      }
    }
    return RSL_STATE;
  }
  if (!strcmp(type, "ra_qos")) {
    if (!q) return RSL_OTHER;
    memset(q, 0, sizeof(*q));
    q->t_ms = (long long)rsl__num(o, e, "t_ms", 0);
    q->window_ms = (int)rsl__num(o, e, "window_ms", 0);
    rsl__svc_qos(rsl__get(o, e, "va"), e, &q->va);
    rsl__svc_qos(rsl__get(o, e, "nonva"), e, &q->nonva);
    size_t c = n < RSL_MAX_DGRAM ? n : RSL_MAX_DGRAM;
    memcpy(q->raw, buf, c);
    q->raw[c] = 0;
    return RSL_QOS;
  }
  return RSL_OTHER;
}

/* Wait up to timeout_ms (0 = no wait) for one datagram from the peer address and parse it.
 * Returns RSL_NONE on timeout, RSL_STATE / RSL_QOS / RSL_OTHER, or -1 (error or malformed datagram). */
static inline int rsl_recv(rsl_link_t* l, int timeout_ms, rsl_state_t* st, rsl_qos_t* q)
{
  struct pollfd p = {.fd = l->fd, .events = POLLIN};
  const int r = poll(&p, 1, timeout_ms);
  if (r < 0) return errno == EINTR ? RSL_NONE : -1;
  if (r == 0) return RSL_NONE;
  char buf[RSL_MAX_DGRAM + 1];
  struct sockaddr_in from;
  socklen_t fl = sizeof(from);
  const ssize_t n = recvfrom(l->fd, buf, RSL_MAX_DGRAM, MSG_DONTWAIT, (struct sockaddr*)&from, &fl);
  if (n <= 0) return n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) ? RSL_NONE : -1;
  if (from.sin_addr.s_addr != l->peer.sin_addr.s_addr) return RSL_OTHER;   /* only the configured host */
  buf[n] = 0;
  return rsl_parse(buf, (size_t)n, st, q);
}

#ifdef __cplusplus
}
#endif
#endif /* RA_SLICING_LINK_H */
