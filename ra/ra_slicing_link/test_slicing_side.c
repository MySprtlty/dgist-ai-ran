/*
 * test_slicing_side.c (ICNL, 2026-11-11): the Slicing xApp's side of INTERFACE.md acted out with ra_slicing_link.h, against
 * ra_stub.py or the real RA. Same rules as xapp_slice_ppo_ctrl.c in PPO_OUTPUT=ra: wait for the four UEs in ra_state, bootstrap
 * 137:136 (applied + associated within 5 s), then every 100 ms a budget when the last ra_state is fresh (<= 200 ms) and
 * consistent, STORED if confirmed within 80 ms.
 *
 *   test_slicing_side [--ra H:P] [--listen H:P] [--va a,b] [--nonva c,d] [--count N] [--expect stored|notconfirmed|hold]
 * Exit 0 when the run matches --expect.
 */
#define _POSIX_C_SOURCE 200809L
#include "ra_slicing_link.h"

static long long mono_ms(void)
{
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static rsl_link_t L;
static rsl_state_t last;          /* latest ra_state */
static long long last_rx = 0;     /* mono ms of its reception */
static unsigned long long n_state = 0, n_qos = 0;
static uint16_t target[4];

static void pump(int ms)          /* receive for up to ms */
{
  const long long until = mono_ms() + ms;
  do {
    rsl_state_t st;
    rsl_qos_t q;
    int left = (int)(until - mono_ms());
    int t = rsl_recv(&L, left > 0 ? left : 0, &st, &q);
    if (t == RSL_STATE) { last = st; last_rx = mono_ms(); n_state++; }
    else if (t == RSL_QOS) n_qos++;
  } while (mono_ms() < until);
}

static int membership(int associated)
{
  int found = 0;
  for (int k = 0; k < 4; k++)
    for (int i = 0; i < last.n_ues; i++)
      if (last.ues[i].rnti == target[k] && (!associated || last.ues[i].svc == k / 2 + 1)) { found++; break; }
  return found == 4 && last.n_ues == 4;
}

static int confirmed(unsigned long long run, unsigned long long seq, unsigned a, unsigned b)
{
  return last.run == run && last.budget_seq == seq && !strcmp(last.status, "applied") && last.has_applied
         && last.applied_va == a && last.applied_nonva == b && membership(1);
}

static int pair(const char* s, uint16_t* out)
{
  unsigned x, y;
  if (!s || sscanf(s, "%x,%x", &x, &y) != 2) return -1;
  out[0] = (uint16_t)x; out[1] = (uint16_t)y;
  return 0;
}

int main(int argc, char** argv)
{
  const char *ra = RSL_DEFAULT_RA, *listen = RSL_DEFAULT_SLICING, *va = "4601,7a3c", *nv = "b210,c0de", *expect = "stored";
  int count = 30;
  for (int i = 1; i + 1 < argc; i += 2) {
    if (!strcmp(argv[i], "--ra")) ra = argv[i + 1];
    else if (!strcmp(argv[i], "--listen")) listen = argv[i + 1];
    else if (!strcmp(argv[i], "--va")) va = argv[i + 1];
    else if (!strcmp(argv[i], "--nonva")) nv = argv[i + 1];
    else if (!strcmp(argv[i], "--count")) count = atoi(argv[i + 1]);
    else if (!strcmp(argv[i], "--expect")) expect = argv[i + 1];
    else { fprintf(stderr, "unknown option %s\n", argv[i]); return 2; }
  }
  if (pair(va, target) || pair(nv, target + 2)) { fprintf(stderr, "--va/--nonva need two hex RNTIs\n"); return 2; }
  if (rsl_open(&L, listen, ra)) { perror("rsl_open"); return 2; }
  const unsigned long long run = (unsigned long long)rsl_wall_ms();
  unsigned long long seq = 0;
  srand((unsigned)run);

  /* 1. start: the four UEs must be visible, then bootstrap 137:136 */
  const long long t_start = mono_ms();
  while (mono_ms() - t_start < 10000 && !(last_rx && membership(0))) pump(10);
  if (!(last_rx && membership(0))) { printf("START_NOT_READY: no ra_state with the four UEs (n_state %llu)\n", n_state); return 1; }
  long long sent = mono_ms();
  if (rsl_send_budget(&L, run, ++seq, 137, 136, 273, 5, target, 2, target + 2, 2)) { perror("send"); return 1; }
  while (mono_ms() - sent < 5000 && !confirmed(run, seq, 137, 136)) pump(5);
  if (!confirmed(run, seq, 137, 136)) { printf("BOOTSTRAP_NOT_CONFIRMED status %s reason '%s'\n", last.status, last.reason); return 1; }
  printf("READY: bootstrap 137:136 confirmed in %lld ms, ra_state every ~%.1f ms so far\n", mono_ms() - sent,
         (double)(mono_ms() - t_start) / (double)(n_state ? n_state : 1));

  /* 2. 100 ms ticks */
  unsigned cur[2] = {137, 136};
  int n_stored = 0, n_notconf = 0, n_hold = 0;
  long long worst = 0, sum = 0;
  long long next = mono_ms() + 100;
  for (int i = 0; i < count; i++) {
    while (mono_ms() < next) pump((int)(next - mono_ms()));
    next += 100;
    const int healthy = last_rx && mono_ms() - last_rx <= 200 && last.has_applied && last.applied_va + last.applied_nonva == 273
                        && membership(1);
    if (!healthy) { n_hold++; printf("tick %2d HOLD (ra_state age %lld ms, status %s)\n", i, mono_ms() - last_rx, last.status); continue; }
    cur[0] = last.applied_va; cur[1] = last.applied_nonva;
    unsigned a = 5 + (unsigned)(rand() % 264);
    if (a == cur[0]) a = a < 268 ? a + 1 : a - 1;
    const unsigned b = 273 - a;
    sent = mono_ms();
    if (rsl_send_budget(&L, run, ++seq, a, b, 273, 5, target, 2, target + 2, 2)) { perror("send"); return 1; }
    while (mono_ms() - sent < 80 && !confirmed(run, seq, a, b)) pump(1);
    if (confirmed(run, seq, a, b)) {
      const long long c = mono_ms() - sent;
      n_stored++; sum += c; if (c > worst) worst = c;
      printf("tick %2d STORED=%u:%u confirm_ms=%lld\n", i, a, b, c);
    } else {
      n_notconf++;
      printf("tick %2d NOT_CONFIRMED %u:%u (status %s, reason '%s', applied %u:%u)\n", i, a, b, last.status, last.reason,
             last.applied_va, last.applied_nonva);
    }
  }
  rsl_send_bye(&L, run);
  rsl_close(&L);
  printf("SUMMARY ticks %d stored %d not_confirmed %d hold %d | confirm mean %.1f max %lld ms | ra_state %llu ra_qos %llu\n", count,
         n_stored, n_notconf, n_hold, n_stored ? (double)sum / n_stored : 0.0, worst, n_state, n_qos);
  if (!strcmp(expect, "stored")) return n_stored == count ? 0 : 1;
  if (!strcmp(expect, "notconfirmed")) return n_notconf == count ? 0 : 1;
  if (!strcmp(expect, "hold")) return n_hold >= count - 3 ? 0 : 1;
  return 2;
}
