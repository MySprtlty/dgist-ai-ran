/* SPDX-License-Identifier: LicenseRef-CSSL-1.0 */
#include "../../../../src/xApp/e42_xapp_api.h"
#include <inttypes.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>
#include <limits.h>
#include <stdarg.h>
#include <sys/stat.h>
#include <ctype.h>

/* Read-only MAC(142) + Slice(145) monitor. No RIC Control, no PPO inference.
 * One E2 node; discover current UEs by RNTI. Never infer services by list order.
 * MAC bytes include MAC overhead: this is NOT PDCP/application throughput. */
#define QUOTA_LABEL "NR_UL_PRB_COUNT_V1"
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static volatile sig_atomic_t stopping;
static mac_ue_stats_impl_t *previous;
static size_t previous_len;
static int64_t previous_ts, last_mac_ms, last_slice_ms;
static int64_t last_display_ms;
static bool display_report = true;
static ue_slice_assoc_t *associations;
static size_t association_len;
static unsigned quotas[2];
static bool quota_valid[2];
/* Configuration-based capacity estimate; the current MAC SM does not carry
 * actual available-slot counters. TDD: 3 full UL slots / 5ms.
 * Override when the RUNNING gNB TDD pattern differs. No symbol weighting. */
static double ul_slots_per_frame = 6.0;
#define UL_MONITOR_TOTAL_PRBS 273
static uint16_t service_rntis[2][2];
static bool manual_mapping;

/* Experiment labels only: never send these associations to the E2 Agent. */
static bool parse_pair(const char *value, uint16_t out[2])
{
  if (!value) return false;
  const char *p = value;
  for (unsigned i = 0; i < 2; ++i) {
    while (isspace((unsigned char)*p)) ++p;
    if (!isxdigit((unsigned char)*p)) return false;
    char *end = NULL;
    errno = 0;
    unsigned long n = strtoul(p, &end, 16);
    if (errno || end == p || n == 0 || n > 0xffef) return false;
    while (isspace((unsigned char)*end)) ++end;
    if (i == 0 ? *end != ',' : *end != '\0') return false;
    out[i] = (uint16_t)n;
    p = i == 0 ? end + 1 : end;
  }
  return out[0] != out[1];
}

static bool mapping_config(void)
{
  const char *va = getenv("VA_RNTIS"), *nv = getenv("NONVA_RNTIS");
  if (!va && !nv) return true;
  if (!parse_pair(va, service_rntis[0]) || !parse_pair(nv, service_rntis[1])) {
    fprintf(stderr, "Set both VA_RNTIS=aaaa,bbbb and NONVA_RNTIS=cccc,dddd (two distinct current hexadecimal C-RNTIs each).\n");
    return false;
  }
  for (unsigned i = 0; i < 2; ++i)
    for (unsigned j = 0; j < 2; ++j)
      if (service_rntis[0][i] == service_rntis[1][j]) {
        fprintf(stderr, "A C-RNTI cannot belong to both services.\n");
        return false;
      }
  manual_mapping = true;
  return true;
}

static unsigned manual_service(uint32_t rnti)
{
  for (unsigned k = 0; k < 2; ++k)
    for (unsigned j = 0; j < 2; ++j)
      if (rnti == service_rntis[k][j]) return k + 1;
  return 0;
}

static double capacity_prbs(int64_t interval_us)
{
  return interval_us > 0 ? UL_MONITOR_TOTAL_PRBS * ul_slots_per_frame * interval_us / 10000.0 : 0.0;
}

static bool capacity_config(void)
{
  const char *s = getenv("UL_MONI_UL_SLOTS_PER_FRAME");
  if (!s) return true;
  char *end = NULL;
  errno = 0;
  double n = strtod(s, &end);
  if (errno || end == s || *end || !isfinite(n) || n <= 0 || n > 160) {
    fprintf(stderr, "UL_MONI_UL_SLOTS_PER_FRAME must be a finite positive UL slot count per 10ms frame (<=160).\n");
    return false;
  }
  ul_slots_per_frame = n;
  return true;
}

static int64_t mono_ms(void)
{
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}


static FILE *text_log;
static char text_log_path[PATH_MAX];
static int64_t run_start_ms;

/* Called with lock held after subscriptions start. Flush each report so an
 * interrupted subscription teardown cannot discard the experiment data. */
static void monitor_print(const char *fmt, ...)
{
  va_list a, b;
  va_start(a, fmt);
  va_copy(b, a);
  if (display_report) vprintf(fmt, a);
  if (text_log) vfprintf(text_log, fmt, b);
  va_end(b);
  va_end(a);
}

static bool open_logs(void)
{
  char exe[PATH_MAX], dir[PATH_MAX];
  ssize_t len = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
  if (len < 0 || len >= (ssize_t)sizeof(exe) - 1) return false;
  exe[len] = 0;
  /* Resolve the workspace from the executable, independent of shell cwd. */
  char *slash = strrchr(exe, '/');
  while (slash && strcmp(slash + 1, "openairinterface5g") != 0) {
    *slash = 0;
    slash = strrchr(exe, '/');
  }
  if (!slash) {
    fprintf(stderr, "Cannot locate openairinterface5g ancestor for log directory\n");
    return false;
  }
  *slash = 0;
  char base[PATH_MAX];
  if (snprintf(base, sizeof(base), "%s/log", exe) >= (int)sizeof(base)) return false;
  if (mkdir(base, 0775) != 0 && errno != EEXIST) { perror(base); return false; }
  if (snprintf(dir, sizeof(dir), "%s/moni_log", base) >= (int)sizeof(dir)) return false;
  if (mkdir(dir, 0775) != 0 && errno != EEXIST) { perror(dir); return false; }
  struct timespec t;
  clock_gettime(CLOCK_REALTIME, &t);
  struct tm local;
  localtime_r(&t.tv_sec, &local);
  char date[32];
  strftime(date, sizeof(date), "%Y%m%d_%H%M%S", &local);
  if (snprintf(text_log_path, sizeof(text_log_path), "%s/ul_input_%s_%ld_%ld.log",
               dir, date, t.tv_nsec, (long)getpid()) >= (int)sizeof(text_log_path)) return false;
  text_log = fopen(text_log_path, "wx");
  if (!text_log) { perror(text_log_path); return false; }
  printf("[UL_INPUT] Text log: %s\n", text_log_path);
  return true;
}

static bool close_logs(void)
{
  if (!text_log) return true;
  bool failed = ferror(text_log) != 0;
  if (fclose(text_log) != 0) failed = true;
  text_log = NULL;
  /* Print before FlexRIC teardown, which can block or abort on a timeout. */
  if (failed)
    fprintf(stderr, "[UL_INPUT] Log write/close error; file may be incomplete: %s\n", text_log_path);
  else
    printf("\n[UL_INPUT] Log saved: %s\n", text_log_path);
  fflush(stdout);
  return !failed;
}

static void stop(int sig)
{
  /* Signal-safe fallback if FlexRIC blocks while removing subscriptions. */
  if (stopping) {
    const char msg[] = "\n[UL_INPUT] Forced monitor exit; subscription cleanup not confirmed.\n";
    (void)write(STDERR_FILENO, msg, sizeof(msg) - 1);
    _exit(128 + sig);
  }
  stopping = 1;
  const char msg[] = "\n[UL_INPUT] Stopping monitor. Press Ctrl+C again to force exit.\n";
  (void)write(STDERR_FILENO, msg, sizeof(msg) - 1);
}

static void on_slice(sm_ag_if_rd_t const *rd)
{
  if (!rd || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != SLICE_STATS_V0)
    return;
  const slice_ind_msg_t *m = &rd->ind.slice.msg;
  size_t n = m->ue_slice_conf.len_ue_slice;
  ue_slice_assoc_t *copy = n ? calloc(n, sizeof(*copy)) : NULL;
  if (n && !copy) return;
  if (n) memcpy(copy, m->ue_slice_conf.ues, n * sizeof(*copy));
  pthread_mutex_lock(&lock);
  free(associations);
  associations = copy;
  association_len = n;
  last_slice_ms = mono_ms();
  memset(quota_valid, 0, sizeof(quota_valid));
  for (unsigned i = 0; i < m->slice_conf.ul.len_slices; i++) {
    const fr_slice_t *s = &m->slice_conf.ul.slices[i];
    if (s->id >= 1 && s->id <= 2 && s->params.type == SLICE_ALG_SM_V0_STATIC
        && s->params.u.sta.pos_low == 0 && s->params.u.sta.pos_high < UL_MONITOR_TOTAL_PRBS
        && s->label && s->len_label == strlen(QUOTA_LABEL)
        && memcmp(s->label, QUOTA_LABEL, strlen(QUOTA_LABEL)) == 0) {
      quotas[s->id - 1] = s->params.u.sta.pos_high + 1;
      quota_valid[s->id - 1] = true;
    }
  }
  pthread_mutex_unlock(&lock);
}

static void on_mac(sm_ag_if_rd_t const *rd)
{
  if (!rd || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != MAC_STATS_V0)
    return;
  const mac_ind_msg_t *m = &rd->ind.mac.msg;
  mac_ue_stats_impl_t *copy = m->len_ue_stats ? calloc(m->len_ue_stats, sizeof(*copy)) : NULL;
  if (m->len_ue_stats && !copy) return;
  if (m->len_ue_stats) memcpy(copy, m->ue_stats, m->len_ue_stats * sizeof(*copy));
  pthread_mutex_lock(&lock);
  if (stopping) { pthread_mutex_unlock(&lock); free(copy); return; }
  int64_t now = mono_ms();
  int64_t dt = m->tstamp - previous_ts;
  /* Installed custom SM parsers support only 1/2/5/10ms. Accumulate
   * counters over approximately 100ms without changing those plugins. */
  if (previous_ts > 0 && dt > 0 && dt < 100000 && now - last_mac_ms < 200) {
    pthread_mutex_unlock(&lock);
    free(copy);
    return;
  }
  bool interval_ok = previous_ts > 0 && dt > 0 && dt <= 200000
                     && last_mac_ms > 0 && now - last_mac_ms <= 200;
  bool slice_fresh = last_slice_ms > 0 && now - last_slice_ms <= 200;
  /* Same UE population and monotonic counters are required for a share. */
  bool population_ok = interval_ok && previous_len == m->len_ue_stats;
  uint64_t total_used = 0;
  for (unsigned i = 0; i < m->len_ue_stats; i++) {
    const mac_ue_stats_impl_t *u = &m->ue_stats[i], *old = NULL;
    for (size_t j = 0; j < previous_len; j++)
      if (previous[j].rnti == u->rnti) { old = &previous[j]; break; }
    if (!old || u->ul_aggr_prb < old->ul_aggr_prb || u->ul_aggr_retx_prb < old->ul_aggr_retx_prb)
      population_ok = false;
    else total_used += (uint64_t)(u->ul_aggr_prb - old->ul_aggr_prb) + (u->ul_aggr_retx_prb - old->ul_aggr_retx_prb);
  }
  double capacity = interval_ok ? capacity_prbs(dt) : 0;
  bool total_ok = population_ok && capacity > 0;
  double total_pct = total_ok ? 100.0 * total_used / capacity : 0;
  /* Keep every ~100ms sample in the file; show only the latest sample each
   * second. Display timing never changes counter baselines or aggregation. */
  display_report = last_display_ms == 0 || now - last_display_ms >= 1000;
  if (display_report) last_display_ms = now;
  monitor_print("\n[UL] UEs=%u window=%.1fms elapsed=%.3fs source_us=%" PRId64 "\n", m->len_ue_stats,
                previous_ts ? dt / 1000.0 : 0.0, (now-run_start_ms)/1000.0, m->tstamp);
  struct service_total {
    unsigned count;
    uint64_t buffer, prbs;
    double tp;
    bool tp_ok, prb_ok;
  } sums[2] = {{.tp_ok = true, .prb_ok = true}, {.tp_ok = true, .prb_ok = true}};
  unsigned unmapped = 0;
  for (unsigned i = 0; i < m->len_ue_stats; i++) {
    const mac_ue_stats_impl_t *u = &m->ue_stats[i], *old = NULL;
    for (size_t j = 0; j < previous_len; j++)
      if (previous[j].rnti == u->rnti) { old = &previous[j]; break; }
    unsigned service = 0;
    bool mapped = false;
    unsigned reported_service = 0;
    bool reported = false;
    if (slice_fresh)
      for (size_t j = 0; j < association_len; j++)
        if (associations[j].rnti == u->rnti) {
          reported_service = associations[j].ul_id;
          reported = true;
          break;
        }
    if (manual_mapping) {
      service = manual_service(u->rnti);
      mapped = service != 0;
    } else {
      service = reported_service;
      mapped = reported;
    }
    bool budget_ok = slice_fresh && reported && reported_service == service && service >= 1 && service <= 2
                     && quota_valid[0] && quota_valid[1]
                     && quotas[0] + quotas[1] == UL_MONITOR_TOTAL_PRBS;
    monitor_print("  UE=%04" PRIx32 " service=", u->rnti);
    if (mapped && service >= 1 && service <= 2)
      monitor_print("%u(%s)", service, service == 1 ? "VA" : "Non-VA");
    else monitor_print("--");
    monitor_print(" service_budget=");
    if (budget_ok) monitor_print("%uPRB", quotas[service - 1]); else monitor_print("--");
    bool counters_ok = old && interval_ok && u->ul_aggr_tbs >= old->ul_aggr_tbs;
    bool prb_ok = old && interval_ok && u->ul_aggr_prb >= old->ul_aggr_prb
                  && u->ul_aggr_retx_prb >= old->ul_aggr_retx_prb;
    uint64_t new_prbs = prb_ok ? u->ul_aggr_prb - old->ul_aggr_prb : 0;
    uint64_t retx_prbs = prb_ok ? u->ul_aggr_retx_prb - old->ul_aggr_retx_prb : 0;
    monitor_print(" PRB_used_avg=");
    if (prb_ok && capacity > 0)
      monitor_print("%.0f", (new_prbs + retx_prbs) * UL_MONITOR_TOTAL_PRBS / capacity);
    else monitor_print("N/A");
    monitor_print(" UL_use273=");
    if (prb_ok && capacity > 0) monitor_print("%.1f%%", 100.0 * (new_prbs + retx_prbs) / capacity);
    else monitor_print("N/A");
    monitor_print(" buffer=%" PRIu32 "B SNR=", u->bsr);
    if (isfinite(u->pusch_snr)) monitor_print("%.2f", (double)u->pusch_snr); else monitor_print("N/A");
    monitor_print("dB UL_MAC=");
    if (counters_ok) monitor_print("%.3f", (u->ul_aggr_tbs - old->ul_aggr_tbs) * 8.0 / dt);
    else monitor_print("N/A");
    monitor_print("Mbps");
    /* A missing budget is simply --; retain actual mapping conflicts. */
    if (manual_mapping && reported && reported_service && reported_service != service)
      monitor_print(" [manual_label_differs_from_agent_service=%u]", reported_service);
    /* Raw counters remain at the end of the file-only record. */
    if (text_log) {
      fprintf(text_log, " UL_wait_ms=");
      if (u->ul_wait_valid) fprintf(text_log, "%.3f", u->ul_wait_us / 1000.0);
      else fprintf(text_log, "N/A");
      fprintf(text_log, " UL_wait_completed_avg_ms=");
      if (interval_ok && old && u->ul_wait_valid && old->ul_wait_valid &&
          u->ul_wait_count > old->ul_wait_count && u->ul_wait_sum_us >= old->ul_wait_sum_us)
        fprintf(text_log, "%.3f", (u->ul_wait_sum_us - old->ul_wait_sum_us) /
                      (1000.0 * (u->ul_wait_count - old->ul_wait_count)));
      else fprintf(text_log, "N/A");
      if (prb_ok && capacity > 0)
        fprintf(text_log, " PRB_used_avg_raw=%.9f", (new_prbs + retx_prbs) * UL_MONITOR_TOTAL_PRBS / capacity);
      fprintf(text_log, " UL_wait_sum_us=%" PRIu64 " UL_wait_count=%" PRIu64,
              u->ul_wait_sum_us, u->ul_wait_count);
      fprintf(text_log, " PRB_slot_sum=");
      if (prb_ok) fprintf(text_log, "%" PRIu64, new_prbs + retx_prbs);
      else fprintf(text_log, "N/A");
    }
    monitor_print("\n");
    if (mapped && service >= 1 && service <= 2) {
      struct service_total *s = &sums[service - 1];
      s->count++;
      s->buffer += u->bsr;
      s->tp_ok &= counters_ok;
      s->prb_ok &= prb_ok;
      if (counters_ok) s->tp += (u->ul_aggr_tbs - old->ul_aggr_tbs) * 8.0 / dt;
      if (prb_ok) s->prbs += new_prbs + retx_prbs;
    } else unmapped++;
  }
  for (unsigned k = 0; k < 2; ++k) {
    struct service_total *s = &sums[k];
    bool complete = s->count > 0 && population_ok && (!manual_mapping || s->count == 2);
    monitor_print("  SERVICE %u(%s) UEs=%u", k + 1, k == 0 ? "VA" : "Non-VA", s->count);
    if (manual_mapping) monitor_print("/2");
    monitor_print(" PRB_avg_per_UL_slot=");
    if (complete && s->prb_ok && capacity > 0)
      monitor_print("%.0f/273", s->prbs * UL_MONITOR_TOTAL_PRBS / capacity);
    else monitor_print("N/A");
    monitor_print(" UL_use273=");
    if (complete && s->prb_ok && capacity > 0) monitor_print("%.1f%%", 100.0 * s->prbs / capacity);
    else monitor_print("N/A");
    monitor_print(" buffer_sum=%" PRIu64 "B UL_MAC_sum=", s->buffer);
    if (complete && s->tp_ok) monitor_print("%.3fMbps", s->tp); else monitor_print("N/A");
    if (!complete) monitor_print(" [incomplete_membership_or_counter_window]");
    if (text_log) {
      fprintf(text_log, " PRB_slot_sum=");
      if (complete && s->prb_ok) fprintf(text_log, "%" PRIu64, s->prbs);
      else fprintf(text_log, "N/A");
    }
    monitor_print("\n");
  }
  monitor_print("  TOTAL reported_UEs=%u unmapped_UEs=%u PRB_avg_per_UL_slot=", m->len_ue_stats, unmapped);
  if (total_ok) monitor_print("%.0f/273", total_used * UL_MONITOR_TOTAL_PRBS / capacity);
  else monitor_print("N/A");
  monitor_print(" UL_use273=");
  if (total_ok) monitor_print("%.1f%%", total_pct); else monitor_print("N/A");
  monitor_print(" unused273=");
  if (total_ok && total_pct <= 100.0) monitor_print("%.1f%%", 100.0-total_pct);
  else monitor_print("N/A");
  if (total_ok && total_pct > 100.0) monitor_print(" [check_TDD_denominator_or_counter_timing]");
  monitor_print("\n");
  display_report = true; /* Startup/status output is not throttled. */
  if (fflush(text_log) || ferror(text_log)) {
    fprintf(stderr, "[UL_INPUT] Log write failed; stopping monitor. Check disk space/permissions.\n");
    stopping = 1;
  }
  free(previous);
  previous = copy;
  previous_len = m->len_ue_stats;
  previous_ts = m->tstamp;
  last_mac_ms = now;
  pthread_mutex_unlock(&lock);
}

int main(int argc, char **argv)
{
  setvbuf(stdout, NULL, _IOLBF, 0);
  fr_args_t args = init_fr_args(argc, argv);
  if (!capacity_config() || !mapping_config()) return EXIT_FAILURE;
  if (!open_logs()) { fprintf(stderr, "Cannot create monitor log file.\n"); return EXIT_FAILURE; }
  run_start_ms = mono_ms();
  init_xapp_api(&args);
  /* init_xapp_api installs its own handlers: ours must be installed AFTER it.
   * Its handler updates a different flag and cannot stop our monitoring loop. */
  struct sigaction action = {0};
  action.sa_handler = stop;
  sigemptyset(&action.sa_mask);
  sigaddset(&action.sa_mask, SIGINT);
  sigaddset(&action.sa_mask, SIGTERM);
  if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL)) {
    perror("sigaction");
    _exit(EXIT_FAILURE);
  }
  sleep(1);
  e2_node_arr_xapp_t nodes = e2_nodes_xapp_api();
  sm_ans_xapp_t mac = {0}, slice = {0};
  int result = 1;
  if (nodes.len != 1) {
    fprintf(stderr, "Exactly one connected E2 node required; found %zu.\n", (size_t)nodes.len);
    goto done;
  }
  bool has_mac = false, has_slice = false;
  for (size_t i = 0; i < nodes.n[0].len_rf; i++) {
    has_mac |= nodes.n[0].rf[i].id == 142;
    has_slice |= nodes.n[0].rf[i].id == 145;
  }
  if (!has_mac) { fprintf(stderr, "MAC SM 142 is required.\n"); goto done; }
  puts("Read-only monitor: MAC/Slice subscription=10ms; counter/log window approximately 100ms; terminal display approximately 1s (latest sample, not a 1s average).");
  puts("Ctrl+C stops monitoring only; it does not change PRB policy.");
  puts("Buffer is max(estimated UL buffer - already scheduled bytes, 0); SNR is last known (measurement age unavailable).");
  puts("TP is MAC RX byte delta / source interval, not application TP. First/reset/gap sample=N/A.");
  monitor_print("Log-file-only PRB_slot_sum=new+retx PRBs summed across the reporting window, NOT a single-slot count.\n");
  if (text_log) fprintf(text_log, "Log-file-only UL_wait_ms: current observed ungranted backlog age; completed average covers grants within the reporting window. Includes DL time; excludes grant-to-PUSCH and UE packet age. N/A completed average means no completed wait sample.\n");
  monitor_print("UE PRB_used_avg=rounded average PRBs used per UL slot; NOT a fixed per-UE budget or exact single-slot grant. Shared service budget is shown separately as service_budget; unknown budget is --.\n");
  monitor_print("Service/TOTAL PRB_avg_per_UL_slot=rounded average/273. Averages include idle UL slots; independently rounded values may not sum exactly. UL_use273 uses unrounded average/273*100.\n");
  monitor_print("Capacity ESTIMATE: total=273, UL slots/10ms frame=%.3f (default TDD=3 full UL slots/5ms, no UL symbols in special slot). Override UL_MONI_UL_SLOTS_PER_FRAME if the running TDD differs.\n", ul_slots_per_frame);
  monitor_print("Unused estimate excludes unreported UEs/RA/Msg3; not symbol-weighted, not exact KPM RRU.PrbTotUl.\n");
  puts("Budget is reported stored policy, not proof of scheduler application; N/A is not zero.");
  if (manual_mapping) {
    monitor_print("Mapping=manual_labels VA=%04x,%04x Non-VA=%04x,%04x; budget requires matching fresh Slice membership and quotas. No policy is changed.\n",
                  (unsigned)service_rntis[0][0], (unsigned)service_rntis[0][1],
                  (unsigned)service_rntis[1][0], (unsigned)service_rntis[1][1]);
    monitor_print("All reported UEs enter TOTAL; unmapped UEs are excluded from service sums. Reconnection may require updated RNTIs.\n");
  } else monitor_print("Mapping=Slice report IDs (1=VA, 2=Non-VA by experiment convention). For PF baseline, set VA_RNTIS and NONVA_RNTIS.\n");
  if (has_slice) {
    slice = report_sm_xapp_api(&nodes.n[0].id, 145, "10_ms", on_slice);
    if (!slice.success) fprintf(stderr, "Slice subscription failed; budgets unavailable.\n");
  } else fprintf(stderr, "Slice SM 145 unavailable; budgets unavailable.\n");
  mac = report_sm_xapp_api(&nodes.n[0].id, 142, "10_ms", on_mac);
  if (!mac.success) goto done;
  result = 0;
  while (!stopping) {
    sleep(1);
    pthread_mutex_lock(&lock);
    if (!last_mac_ms || mono_ms() - last_mac_ms > 500)
      puts("[UL_INPUT] No fresh MAC report: check UE connection, E2 Agent and RIC.");
    pthread_mutex_unlock(&lock);
  }
done:
  stopping = 1;
  pthread_mutex_lock(&lock);
  if (!close_logs()) result = EXIT_FAILURE;
  pthread_mutex_unlock(&lock);
  if (mac.success) rm_report_sm_xapp_api(mac.u.handle);
  if (slice.success) rm_report_sm_xapp_api(slice.u.handle);
  while (!try_stop_xapp_api()) usleep(1000);
  free_e2_node_arr_xapp(&nodes);
  free(previous);
  free(associations);
  return result;
}

