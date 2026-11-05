/* SPDX-License-Identifier: LicenseRef-CSSL-1.0 */
#include "../../../../src/xApp/e42_xapp_api.h"
#include "../../../../src/sm/slice_sm/slice_sm_id.h"
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <inttypes.h>
#include <sys/stat.h>
#include <limits.h>
#include <stdarg.h>
#include <fcntl.h>

/* Explicit current RNTIs, not list order or AMF UE IDs. This does not classify
 * application traffic or map S-NSSAI. Two UEs per experimental slice, same BWP.
 * Versioned count quotas, total 273; no borrowing. Both new TX and HARQ count. */
#define QUOTA_LABEL "NR_UL_PRB_COUNT_V1"
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static volatile sig_atomic_t stopping;
static uint16_t target[4]; /* VA pair, then Non-VA pair */
static struct {
  uint64_t reports;
  int64_t received_ms;
  unsigned ue_count, slice_count;
  uint16_t observed[4];
  bool found[4], any_assoc;
  uint32_t assoc[4];
  unsigned start[2], prbs[2];
} state;

static int64_t now_ms(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* Log our control/diagnostic messages, not all FlexRIC/Python stdout.
 * Open before fork/threads. Only the control thread calls these helpers. */
static FILE *quota_log_file;
static char quota_log_path[PATH_MAX];
static int64_t log_start_ms;
static bool log_failed;

static void close_quota_log(void)
{
  if (!quota_log_file) return;
  if (ferror(quota_log_file)) log_failed = true;
  if (fclose(quota_log_file) != 0) log_failed = true;
  quota_log_file = NULL;
  if (log_failed)
    fprintf(stderr, "[SLICE_XAPP] Log may be incomplete (write/close error): %s\n", quota_log_path);
  else
    printf("\n[SLICE_XAPP] Log saved: %s\n", quota_log_path);
  fflush(stdout);
}

static void quota_print(FILE *console, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));
static void quota_print(FILE *console, const char *fmt, ...)
{
  va_list args, copy;
  va_start(args, fmt);
  va_copy(copy, args);
  vfprintf(console, fmt, args);
  va_end(args);
  if (quota_log_file && !log_failed) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    fprintf(quota_log_file, "[wall_us=%" PRId64 " elapsed_ms=%" PRId64 "] ",
            (int64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000, now_ms() - log_start_ms);
    vfprintf(quota_log_file, fmt, copy);
    if (fflush(quota_log_file) != 0 || ferror(quota_log_file)) {
      log_failed = true;
      stopping = 1;
      fprintf(stderr, "[SLICE_XAPP] Log write failed; stopping control. File: %s\n", quota_log_path);
    }
  }
  va_end(copy);
}

#define quota_info(...) quota_print(stdout, __VA_ARGS__)
#define quota_error(...) quota_print(stderr, __VA_ARGS__)
#define quota_line(message) quota_info("%s\n", (message))

static bool open_quota_log(void)
{
  char exe[PATH_MAX], dir[PATH_MAX], date[32];
  ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
  if (n <= 0 || (size_t)n >= sizeof(exe) - 1) return false;
  exe[n] = 0;
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
  if (snprintf(dir, sizeof(dir), "%s/ppo_output_log", base) >= (int)sizeof(dir)) return false;
  if (mkdir(dir, 0775) != 0 && errno != EEXIST) { perror(dir); return false; }
  struct timespec t;
  struct tm local;
  clock_gettime(CLOCK_REALTIME, &t);
  if (!localtime_r(&t.tv_sec, &local)
      || !strftime(date, sizeof(date), "%Y%m%d_%H%M%S", &local)) return false;
  if (snprintf(quota_log_path, sizeof(quota_log_path), "%s/quota_control_%s_%ld_%ld.log",
               dir, date, t.tv_nsec, (long)getpid()) >= (int)sizeof(quota_log_path)) return false;
  quota_log_file = fopen(quota_log_path, "wx");
  if (!quota_log_file) { perror(quota_log_path); return false; }
  /* The Python subprocess must not keep the log descriptor open after exec. */
  if (fcntl(fileno(quota_log_file), F_SETFD, FD_CLOEXEC) < 0) {
    perror("log FD_CLOEXEC");
    log_failed = true;
    close_quota_log();
    return false;
  }
  log_start_ms = now_ms();
  if (atexit(close_quota_log) != 0) { close_quota_log(); return false; }
  quota_info("[SLICE_XAPP] Log file: %s\n", quota_log_path);
  return !log_failed;
}

static void stop(int sig) { if (stopping) _exit(128 + sig); stopping = 1; }

static long long wall_ms(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void on_report(sm_ag_if_rd_t const *rd)
{
  if (!rd || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != SLICE_STATS_V0)
    return;
  const slice_ind_msg_t *m = &rd->ind.slice.msg;
  pthread_mutex_lock(&lock);
  state.reports++;
  state.received_ms = now_ms();
  state.ue_count = m->ue_slice_conf.len_ue_slice;
  memset(state.observed, 0, sizeof(state.observed));
  for (unsigned i = 0; i < state.ue_count && i < 4; i++)
    state.observed[i] = m->ue_slice_conf.ues[i].rnti;
  state.slice_count = m->slice_conf.ul.len_slices;
  state.any_assoc = false;
  memset(state.start, 0, sizeof(state.start));
  memset(state.prbs, 0, sizeof(state.prbs));
  for (unsigned k = 0; k < 4; k++) {
    state.found[k] = false;
    state.assoc[k] = 0;
  }
  for (unsigned i = 0; i < m->ue_slice_conf.len_ue_slice; i++) {
    const ue_slice_assoc_t *u = &m->ue_slice_conf.ues[i];
    state.any_assoc |= u->ul_id != 0;
    for (unsigned k = 0; k < 4; k++)
      if (u->rnti == target[k]) {
        state.found[k] = true;
        state.assoc[k] = u->ul_id;
      }
  }
  for (unsigned i = 0; i < m->slice_conf.ul.len_slices; i++) {
    const fr_slice_t *s = &m->slice_conf.ul.slices[i];
    if (s->id >= 1 && s->id <= 2 && s->params.type == SLICE_ALG_SM_V0_STATIC
        && s->params.u.sta.pos_low == 0 && s->params.u.sta.pos_high < 273
        && s->len_label == strlen(QUOTA_LABEL) && s->label
        && memcmp(s->label, QUOTA_LABEL, strlen(QUOTA_LABEL)) == 0) {
      state.start[s->id - 1] = s->params.u.sta.pos_low;
      state.prbs[s->id - 1] = s->params.u.sta.pos_high - s->params.u.sta.pos_low + 1;
    }
  }
  pthread_mutex_unlock(&lock);
}

static uint64_t report_count(void)
{
  pthread_mutex_lock(&lock);
  uint64_t n = state.reports;
  pthread_mutex_unlock(&lock);
  return n;
}

/* Must be called under lock. Confirm stored state, not successful radio TX. */
static bool matches(unsigned first, unsigned second, bool associated)
{
  for (unsigned k = 0; k < 4; k++)
    if (!state.found[k] || state.assoc[k] != (associated ? k / 2 + 1 : 0)) return false;
  return state.ue_count == 4
         && state.slice_count == 2 && state.start[0] == 0 && state.start[1] == 0
         && state.prbs[0] == first && state.prbs[1] == second;
}

static bool confirm(uint64_t after, unsigned first, unsigned second, bool associated, bool cleanup)
{
  int64_t deadline = now_ms() + 5000;
  while ((cleanup || !stopping) && now_ms() < deadline) {
    pthread_mutex_lock(&lock);
    bool ok = state.reports > after && (cleanup ? state.slice_count == 0 && !state.any_assoc
                                               : matches(first, second, associated));
    pthread_mutex_unlock(&lock);
    if (ok) return true;
    usleep(10000);
  }
  quota_error( "[SLICE_XAPP] Confirmation timeout; check RIC and E2_SLICE_REJECT.\n");
  return false;
}

static bool quotas(global_e2_node_id_t *id, unsigned first, unsigned second)
{
  fr_slice_t slices[2] = {0};
  for (unsigned k = 0; k < 2; k++) {
    slices[k].id = k + 1;
    slices[k].params.type = SLICE_ALG_SM_V0_STATIC;
    slices[k].label = QUOTA_LABEL;
    slices[k].len_label = strlen(QUOTA_LABEL);
  }
  slices[0].params.u.sta.pos_high = first - 1;
  slices[1].params.u.sta.pos_low = 0;
  slices[1].params.u.sta.pos_high = second - 1;
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_ADD;
  req.msg.u.add_mod_slice.ul.len_slices = 2;
  req.msg.u.add_mod_slice.ul.slices = slices;
  quota_info("[SLICE_XAPP] send_ms=%lld COUNT_QUOTAS total=273 service1=%u service2=%u no_lending=1\n",
         (long long)now_ms(), first, second);
  return control_sm_xapp_api(id, SM_SLICE_ID, &req).success;
}

static bool associate(global_e2_node_id_t *id)
{
  ue_slice_assoc_t members[4] = {0};
  for (unsigned k = 0; k < 4; k++)
    members[k] = (ue_slice_assoc_t){.rnti = target[k], .ul_id = k / 2 + 1};
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_UE_SLICE_ASSOC;
  req.msg.u.ue_slice.len_ue_slice = 4;
  req.msg.u.ue_slice.ues = members;
  quota_info("[SLICE_XAPP] ASSOC_FOUR VA=%04x,%04x NonVA=%04x,%04x\n", target[0], target[1], target[2], target[3]);
  return control_sm_xapp_api(id, SM_SLICE_ID, &req).success;
}

static bool remove_pair(global_e2_node_id_t *id)
{
  uint32_t ids[2] = {1, 2};
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_DEL;
  req.msg.u.del_slice.len_ul = 2;
  req.msg.u.del_slice.ul = ids;
  return control_sm_xapp_api(id, SM_SLICE_ID, &req).success;
}

static bool read_pair(const char *name, uint16_t *value)
{
  const char *env = getenv(name);
  for (unsigned k = 0; k < 2; k++) {
    char *end = NULL;
    errno = 0;
    unsigned long n = env ? strtoul(env, &end, 16) : 0;
    if (!env || end == env || errno || n == 0 || n >= 0xfff0 || (k == 0 ? *end != ',' : *end != '\0')) {
      quota_error( "%s requires two current hexadecimal C-RNTIs, e.g. 1234,2345.\n", name);
      return false;
    }
    value[k] = n;
    env = k == 0 ? end + 1 : NULL;
  }
  return true;
}

static bool read_unsigned_env(const char *name, unsigned minimum, unsigned maximum, unsigned *out)
{
  const char *text = getenv(name);
  char *end = NULL;
  errno = 0;
  unsigned long value = text ? strtoul(text, &end, 10) : 0;
  if (!text || *text < '0' || *text > '9' || end == text || *end || errno
      || value < minimum || value > maximum) {
    quota_error( "%s must be an integer in [%u,%u].\n", name, minimum, maximum);
    return false;
  }
  *out = (unsigned)value;
  return true;
}

int main(int argc, char **argv)
{
  if (!read_pair("VA_RNTIS", &target[0]) || !read_pair("NONVA_RNTIS", &target[2]))
    return 1;
  for (unsigned i = 0; i < 4; i++)
    for (unsigned j = 0; j < i; j++)
      if (target[i] == target[j]) { quota_error( "All four RNTIs must be distinct.\n"); return 1; }
  /* VA:Non-VA = 20:80, 50:50, 80:20. Keep the integer sum at 273;
   * the odd PRB in the middle stage belongs to VA. */
  unsigned policy[7][2] = {{55, 218}, {137, 136}, {218, 55}};
  unsigned hold_ms = 20000, stages = 3;
  const char *mode = getenv("SLICE_MODE");
  const bool fixed = mode && strcmp(mode, "fixed") == 0;
  const bool sweep = mode && strcmp(mode, "sweep") == 0;
  const bool calibration = mode && strcmp(mode, "calibration") == 0;
  if (mode && !fixed && !sweep && !calibration && strcmp(mode, "timed") != 0) {
    quota_error( "SLICE_MODE must be fixed, timed, sweep, or calibration.\n");
    return 1;
  }
  if (fixed) {
    if (!read_unsigned_env("SLICE1_PRBS", 5, 268, &policy[0][0])) return 1;
    policy[0][1] = 273 - policy[0][0];
    stages = 1;
    hold_ms = 0; /* Hold until Ctrl+C, unless an explicit duration is supplied. */
    if (getenv("SLICE_HOLD_MS")
        && !read_unsigned_env("SLICE_HOLD_MS", 0, 3600000, &hold_ms)) return 1;
  } else if (calibration) {
    const unsigned va[] = {220, 210, 200, 190, 200, 210, 220};
    stages = 7;
    hold_ms = 20000;
    for (unsigned i = 0; i < stages; i++) {
      policy[i][0] = va[i];
      policy[i][1] = 273 - va[i];
    }
    if (getenv("SLICE1_PRBS") || getenv("SLICE_HOLD_MS")) {
      quota_error( "Calibration uses seven predefined quotas and 20s per stage; unset SLICE1_PRBS/SLICE_HOLD_MS.\n");
      return 1;
    }
  } else if (sweep) {
    stages = 4;
    hold_ms = 30000;
    for (unsigned i = 0; i < stages; i++) {
      policy[i][0] = 160 + 20 * i;
      policy[i][1] = 273 - policy[i][0];
    }
    if (getenv("SLICE1_PRBS") || getenv("SLICE_HOLD_MS")) {
      quota_error( "Sweep uses four predefined quotas and 30s per stage; unset SLICE1_PRBS/SLICE_HOLD_MS.\n");
      return 1;
    }
  } else if (getenv("SLICE1_PRBS") || getenv("SLICE_HOLD_MS"))
    quota_error( "[SLICE_XAPP] SLICE1_PRBS/SLICE_HOLD_MS ignored: timed 3-stage experiment, 20 seconds each.\n");
  setvbuf(stdout, NULL, _IOLBF, 0);
  if (!open_quota_log()) { fprintf(stderr, "Cannot create quota log; no control started.\n"); return 1; }
  fr_args_t args = init_fr_args(argc, argv);
  init_xapp_api(&args);
  signal(SIGINT, stop);
  signal(SIGTERM, stop);
  sleep(1);
  e2_node_arr_xapp_t nodes = e2_nodes_xapp_api();
  sm_ans_xapp_t sub = {0};
  bool subscribed = false, touched = false;
  int result = 1;
  if (nodes.len != 1) {
    quota_error( "Exactly one E2 node required. No Control sent.\n");
    goto done;
  }
  bool supported = false;
  for (size_t i = 0; i < nodes.n[0].len_rf; i++)
    supported |= nodes.n[0].rf[i].id == SM_SLICE_ID;
  if (!supported) goto done;
  sub = report_sm_xapp_api(&nodes.n[0].id, SM_SLICE_ID, "10_ms", on_report);
  if (!sub.success) goto done;
  subscribed = true;
  int64_t deadline = now_ms() + 10000;
  bool ready = false;
  while (!stopping && now_ms() < deadline) {
    pthread_mutex_lock(&lock);
    bool existing = state.reports && (state.slice_count || state.any_assoc);
    ready = state.reports && state.ue_count == 4;
    for (unsigned k = 0; k < 4; k++) ready &= state.found[k];
    pthread_mutex_unlock(&lock);
    if (existing) {
      quota_error( "Existing policy detected; refusing to overwrite. No Control sent.\n");
      goto done;
    }
    if (ready) break;
    usleep(10000);
  }
  if (!ready || stopping) {
    pthread_mutex_lock(&lock);
    quota_error( "[SLICE_XAPP] UE check: reports=%llu reported_UEs=%u age_ms=%lld\n",
            (unsigned long long)state.reports, state.ue_count,
            state.reports ? (long long)(now_ms() - state.received_ms) : -1LL);
    quota_error( "[SLICE_XAPP] Reported RNTIs (first four): %04x,%04x,%04x,%04x\n",
            state.observed[0], state.observed[1], state.observed[2], state.observed[3]);
    for (unsigned k = 0; k < 4; k++)
      quota_error( "[SLICE_XAPP] Expected %04x: %s\n", target[k], state.found[k] ? "found" : "missing");
    pthread_mutex_unlock(&lock);
    quota_error( "Four connected UEs matching VA_RNTIS/NONVA_RNTIS required. No Control sent.\n");
    goto done;
  }
  if (fixed)
    quota_info("[SLICE_XAPP] FIXED VA:Non-VA=%u:%u hold_ms=%u (0=until Ctrl+C). Two UEs SHARE each service count; existing PF within service.\n",
           policy[0][0], policy[0][1], hold_ms);
  else if (calibration)
    quota_info("[SLICE_XAPP] CALIBRATION VA quotas 220 -> 210 -> 200 -> 190 -> 200 -> 210 -> 220; Non-VA=273-VA; 20s each, 140s total, then delete quotas. Two UEs SHARE each service count; existing PF within service.\n");
  else if (sweep)
    quota_info("[SLICE_XAPP] SWEEP VA:Non-VA 160:113 -> 180:93 -> 200:73 -> 220:53; 30s each, 120s total, then delete quotas. Two UEs SHARE each service count; existing PF within service.\n");
  else
    quota_info("[SLICE_XAPP] Timed VA:Non-VA quotas 55:218 -> 137:136 -> 218:55; 20s each, then delete quotas. Two UEs SHARE each service count; existing PF within service.\n");
  int64_t experiment_start_ms = 0;
  for (unsigned stage = 0; stage < stages; stage++) {
    if (stopping) goto done;
    uint64_t before = report_count();
    touched = true;
    if (!quotas(&nodes.n[0].id, policy[stage][0], policy[stage][1])
        || !confirm(before, policy[stage][0], policy[stage][1], stage != 0, false)) goto done;
    if (stage == 0) {
      before = report_count();
      if (!associate(&nodes.n[0].id) || !confirm(before, policy[stage][0], policy[stage][1], true, false)) goto done;
    }
    if (stage == 0) experiment_start_ms = now_ms();
    /* Absolute deadlines avoid adding each control/confirmation delay to the
     * 60s experiment. The radio applies changes after any HARQ drain. */
    deadline = experiment_start_ms + (int64_t)(stage + 1) * hold_ms;
    quota_info("[SLICE_XAPP] STORED wall_ms=%lld stage=%u elapsed_ms=%lld service1=%u service2=%u stage_end_ms=%u; verify per-UE E2_SLICE_APPLIED\n",
           wall_ms(), stage + 1, (long long)(now_ms() - experiment_start_ms),
           policy[stage][0], policy[stage][1], (stage + 1) * hold_ms);
    if (hold_ms && now_ms() >= deadline) {
      quota_error( "[SLICE_XAPP] Stage deadline missed; ending experiment.\n");
      goto done;
    }
    while (!stopping && (!hold_ms || now_ms() < deadline)) {
      pthread_mutex_lock(&lock);
      bool healthy = matches(policy[stage][0], policy[stage][1], true) && now_ms() - state.received_ms < 2000;
      pthread_mutex_unlock(&lock);
      if (!healthy) {
        quota_error( "UE membership/policy changed or reports stopped; ending experiment.\n");
        goto done;
      }
      usleep(10000);
    }
  }
  result = 0;
done:
  if (touched) {
    uint64_t before = report_count();
    if (!remove_pair(&nodes.n[0].id) || !confirm(before, 0, 0, false, true)) {
      quota_error( "RESTORE NOT CONFIRMED. Restart OAI if policy remains active.\n");
      result = 1;
    } else quota_info("[SLICE_XAPP] both quotas deleted; shared total-273 baseline restored\n");
  }
  if (subscribed) rm_report_sm_xapp_api(sub.u.handle);
  while (!try_stop_xapp_api()) usleep(1000);
  free_e2_node_arr_xapp(&nodes);
  return result;
}

