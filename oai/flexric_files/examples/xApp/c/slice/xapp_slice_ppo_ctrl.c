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
#include <math.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <limits.h>
#include <stdarg.h>
#include <fcntl.h>

/* Explicit current RNTIs, not list order or AMF UE IDs. This does not classify
 * application traffic or map S-NSSAI. Two UEs per experimental slice, same BWP.
 * VA_RNTIS=a,b and NONVA_RNTIS=c,d define the stable model input order.
 * All four UEs remain connected; traffic may turn on/off independently.
 * Versioned count quotas, total 273; no borrowing. Both new TX and HARQ count. */
#define QUOTA_LABEL "NR_UL_PRB_COUNT_V1"
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static volatile sig_atomic_t stopping;
/* Explicit opt-in keeps the existing GPU/latency PPO4 bundles usable. */
static bool radio_inputs;
static uint16_t target[4];
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
static FILE *ppo_log_file;
static char ppo_log_path[PATH_MAX];
static int64_t log_start_ms;
static bool log_failed;

static void close_ppo_log(void)
{
  if (!ppo_log_file) return;
  if (ferror(ppo_log_file)) log_failed = true;
  if (fclose(ppo_log_file) != 0) log_failed = true;
  ppo_log_file = NULL;
  if (log_failed)
    fprintf(stderr, "[PPO] Log may be incomplete (write/close error): %s\n", ppo_log_path);
  else
    printf("\n[PPO] Log saved: %s\n", ppo_log_path);
  fflush(stdout);
}

static void ppo_print(FILE *console, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));
static void ppo_print(FILE *console, const char *fmt, ...)
{
  va_list args, copy;
  va_start(args, fmt);
  va_copy(copy, args);
  vfprintf(console, fmt, args);
  va_end(args);
  if (ppo_log_file && !log_failed) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    fprintf(ppo_log_file, "[wall_us=%" PRId64 " elapsed_ms=%" PRId64 "] ",
            (int64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000, now_ms() - log_start_ms);
    vfprintf(ppo_log_file, fmt, copy);
    if (fflush(ppo_log_file) != 0 || ferror(ppo_log_file)) {
      log_failed = true;
      stopping = 1;
      fprintf(stderr, "[PPO] Log write failed; stopping control. File: %s\n", ppo_log_path);
    }
  }
  va_end(copy);
}

#define ppo_info(...) ppo_print(stdout, __VA_ARGS__)
#define ppo_error(...) ppo_print(stderr, __VA_ARGS__)
#define ppo_line(message) ppo_info("%s\n", (message))

static bool open_ppo_log(void)
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
  if (snprintf(ppo_log_path, sizeof(ppo_log_path), "%s/ppo_output_%s_%ld_%ld.log",
               dir, date, t.tv_nsec, (long)getpid()) >= (int)sizeof(ppo_log_path)) return false;
  ppo_log_file = fopen(ppo_log_path, "wx");
  if (!ppo_log_file) { perror(ppo_log_path); return false; }
  /* The Python subprocess must not keep the log descriptor open after exec. */
  if (fcntl(fileno(ppo_log_file), F_SETFD, FD_CLOEXEC) < 0) {
    perror("log FD_CLOEXEC");
    log_failed = true;
    close_ppo_log();
    return false;
  }
  log_start_ms = now_ms();
  if (atexit(close_ppo_log) != 0) { close_ppo_log(); return false; }
  ppo_info("[PPO] Log file: %s\n", ppo_log_path);
  return !log_failed;
}

static void stop(int sig)
{
  if (stopping) _exit(128 + sig);
  stopping = 1;
  const char msg[] = "\n[PPO] Stopping; Ctrl+C again forces exit without confirmed cleanup.\n";
  (void)write(STDERR_FILENO, msg, sizeof(msg) - 1);
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
  ppo_error("[SLICE_XAPP] Confirmation timeout; check RIC and E2_SLICE_REJECT.\n");
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
  ppo_info("[SLICE_XAPP] send_ms=%lld COUNT_QUOTAS total=273 service1=%u service2=%u no_lending=1\n",
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
  ppo_info("[SLICE_XAPP] ASSOC_FOUR VA=%04x,%04x NonVA=%04x,%04x\n", target[0], target[1], target[2], target[3]);
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
      ppo_error( "%s requires two current hexadecimal C-RNTIs, e.g. 1234,2345.\n", name);
      return false;
    }
    value[k] = n;
    env = k == 0 ? end + 1 : NULL;
  }
  return true;
}

/* MAC input snapshot. The same mutex protects it and the Slice state above. */
typedef struct {
  int64_t source_us, received_ms;
  bool valid;
  mac_ue_stats_impl_t ue[4];
} telemetry_t;
static telemetry_t telemetry;

static void on_mac(sm_ag_if_rd_t const *rd)
{
  if (!rd || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != MAC_STATS_V0) return;
  const mac_ind_msg_t *m = &rd->ind.mac.msg;
  telemetry_t next = {.source_us=m->tstamp, .received_ms=now_ms()};
  bool found[4] = {0};
  for (unsigned i=0; i<m->len_ue_stats; i++)
    for (unsigned k=0; k<4; k++)
      if (m->ue_stats[i].rnti == target[k]) {
        next.ue[k] = m->ue_stats[i];
        found[k] = true;
      }
  next.valid = m->len_ue_stats == 4 && found[0] && found[1] && found[2] && found[3];
  pthread_mutex_lock(&lock);
  telemetry = next;
  pthread_mutex_unlock(&lock);
}

static int64_t wall_ms(void)
{
  struct timespec t;
  clock_gettime(CLOCK_REALTIME, &t);
  return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

/* Request/response deadline, discard old sequence IDs after timeouts.
 * A datagram cannot be partially read and confused with the next result. */
static bool infer(int fd, const struct sockaddr_un *server, uint64_t seq,
                  const telemetry_t *cur, const telemetry_t *prev,
                  const unsigned current[2], unsigned minimum, unsigned result[2])
{
  int64_t dt = cur->source_us - prev->source_us;
  if (!cur->valid || !prev->valid || dt < 80000 || dt > 120000
      || now_ms() - cur->received_ms > 200
      || wall_ms() - cur->source_us/1000 > 200
      || cur->source_us/1000 - wall_ms() > 5) return false;
  double tp[4], prb_avg[4] = {0};
  for (unsigned k=0; k<4; k++) {
    if (cur->ue[k].ul_aggr_tbs < prev->ue[k].ul_aggr_tbs
        || !isfinite(cur->ue[k].pusch_snr)
        || cur->ue[k].pusch_snr < -50 || cur->ue[k].pusch_snr > 80)
      return false;
    tp[k] = (cur->ue[k].ul_aggr_tbs - prev->ue[k].ul_aggr_tbs) * 8.0 / dt;
    if (radio_inputs) {
      if (cur->ue[k].ul_aggr_prb < prev->ue[k].ul_aggr_prb ||
          cur->ue[k].ul_aggr_retx_prb < prev->ue[k].ul_aggr_retx_prb) return false;
      const uint64_t used = (uint64_t)(cur->ue[k].ul_aggr_prb - prev->ue[k].ul_aggr_prb)
                         + (cur->ue[k].ul_aggr_retx_prb - prev->ue[k].ul_aggr_retx_prb);
      /* Same denominator as moni: 6 full UL slots per 10ms, DDDDDDSUUU.
       * Counts include retransmissions; do not round policy inputs. */
      prb_avg[k] = used / (dt * 6.0 / 10000.0);
      if (!isfinite(prb_avg[k]) || prb_avg[k] > 274.) return false;
    }
  }
  char request[512];
  int len;
  if (radio_inputs)
    len = snprintf(request, sizeof(request),
      "PPO4R %" PRIu64 " %" PRId64 " %.3f %" PRIu32 " %.6f %.9f %.9f %" PRIu32 " %.6f %.9f %.9f %" PRIu32 " %.6f %.9f %.9f %" PRIu32 " %.6f %.9f %.9f %u %u\n",
      seq, cur->source_us/1000, dt/1000.0,
      cur->ue[0].bsr, (double)cur->ue[0].pusch_snr, tp[0], prb_avg[0],
      cur->ue[1].bsr, (double)cur->ue[1].pusch_snr, tp[1], prb_avg[1],
      cur->ue[2].bsr, (double)cur->ue[2].pusch_snr, tp[2], prb_avg[2],
      cur->ue[3].bsr, (double)cur->ue[3].pusch_snr, tp[3], prb_avg[3], current[0], current[1]);
  else len = snprintf(request, sizeof(request),
      "PPO4 %" PRIu64 " %" PRId64 " %.3f %" PRIu32 " %.6f %.9f %" PRIu32 " %.6f %.9f %" PRIu32 " %.6f %.9f %" PRIu32 " %.6f %.9f %u %u\n",
      seq, cur->source_us/1000, dt/1000.0,
      cur->ue[0].bsr, (double)cur->ue[0].pusch_snr, tp[0],
      cur->ue[1].bsr, (double)cur->ue[1].pusch_snr, tp[1],
      cur->ue[2].bsr, (double)cur->ue[2].pusch_snr, tp[2],
      cur->ue[3].bsr, (double)cur->ue[3].pusch_snr, tp[3], current[0], current[1]);
  if (len <= 0 || (size_t)len >= sizeof(request)
      || sendto(fd, request, len, MSG_DONTWAIT, (const struct sockaddr *)server, sizeof(*server)) != len)
    return false;
  int64_t deadline = now_ms() + 50;
  while (!stopping && now_ms() < deadline) {
    struct pollfd p = {.fd=fd, .events=POLLIN};
    int wait = (int)(deadline - now_ms());
    if (wait <= 0) break;
    if (poll(&p, 1, wait) <= 0) continue;
    char reply[128], status[8], extra;
    struct sockaddr_un from = {0};
    socklen_t from_len = sizeof(from);
    ssize_t n = recvfrom(fd, reply, sizeof(reply)-1, MSG_DONTWAIT, (struct sockaddr *)&from, &from_len);
    if (n <= 0 || strncmp(from.sun_path, server->sun_path, sizeof(from.sun_path)) != 0) continue;
    reply[n] = 0;
    uint64_t got=0, inference_us=0;
    unsigned a=0, b=0;
    if (sscanf(reply, "%" SCNu64 " %7s %u %u %" SCNu64 " %c",
               &got, status, &a, &b, &inference_us, &extra) != 5 || got != seq) continue;
    if (strcmp(status, "OK") || a < minimum || b < minimum || a > 273 || b > 273 || a+b != 273)
      return false;
    if (now_ms() > deadline || wall_ms() - cur->source_us/1000 > 200) return false;
    result[0]=a; result[1]=b;
    ppo_info("[PPO] seq=%" PRIu64 " input_window=%.3fms inferred=%u:%u infer_us=%" PRIu64
             " source_us=%" PRId64 " reported=%u:%u\n",
           seq, dt/1000.0, a, b, inference_us, cur->source_us, current[0], current[1]);
    return true;
  }
  return false;
}

static pid_t python_pid;
static char owned_directory[PATH_MAX], owned_server[108], owned_client[108];

static void cleanup_python(void)
{
  if (python_pid > 0) {
    int status;
    pid_t r=waitpid(python_pid,&status,WNOHANG);
    if (r==0) {
      kill(python_pid,SIGTERM);
      for(unsigned i=0;i<100 && (r=waitpid(python_pid,&status,WNOHANG))==0;i++) usleep(20000);
      if(r==0) {
        kill(python_pid,SIGKILL);
        while(waitpid(python_pid,&status,0)<0 && errno==EINTR) {}
      }
    }
    python_pid=0;
  }
  if(owned_client[0]) unlink(owned_client);
  if(owned_server[0]) unlink(owned_server);
  if(owned_directory[0]) rmdir(owned_directory);
}

/* Resolve relative to the executable, not the terminal's current directory.
 * Walk up to 25_3_hsw, which contains both openairinterface5g and the bundle. */
static bool find_bundle(char bundle[PATH_MAX])
{
  const char *override=getenv("PPO_BUNDLE");
  if(override) return realpath(override,bundle)!=NULL;
  char ancestor[PATH_MAX];
  ssize_t n=readlink("/proc/self/exe",ancestor,sizeof(ancestor)-1);
  if(n<=0 || (size_t)n>=sizeof(ancestor)-1) return false;
  ancestor[n]=0;
  for(char *slash=strrchr(ancestor,'/'); slash; slash=strrchr(ancestor,'/')) {
    *slash=0;
    int len=snprintf(bundle,PATH_MAX,"%s/xapp_basic_no_lending",ancestor);
    char script[PATH_MAX];
    int slen=snprintf(script,sizeof(script),"%s/ppo_socket_server.py",bundle);
    if(len>0 && len<PATH_MAX && slen>0 && (size_t)slen<sizeof(script) && access(script,R_OK)==0)
      return true;
  }
  return false;
}

static bool start_python(const char *bundle, const char *socket_path, unsigned minimum, int client_fd)
{
  char script[PATH_MAX], min_text[16];
  int len=snprintf(script,sizeof(script),"%s/ppo_socket_server.py",bundle);
  if(len<=0 || (size_t)len>=sizeof(script) || access(script,R_OK)) return false;
  snprintf(min_text,sizeof(min_text),"%u",minimum);
  const char *python=getenv("PPO_PYTHON");
  if(!python || !python[0]) python="python3";
  pid_t parent=getpid();
  python_pid=fork(); /* before init_xapp_api creates worker threads */
  if(python_pid<0) { python_pid=0; perror("fork Python"); return false; }
  if(python_pid==0) {
    /* Parent alone owns Ctrl+C. Even a killed/crashed xApp must not orphan the model. */
    if(setpgid(0,0)<0 || prctl(PR_SET_PDEATHSIG,SIGKILL)<0 || getppid()!=parent) _exit(126);
    close(client_fd);
    execlp(python,python,"-u",script,"--bundle",bundle,"--socket",socket_path,"--min-prbs",min_text,(char *)NULL);
    perror("exec Python (set PPO_PYTHON to your PyTorch environment)");
    _exit(127);
  }
  ppo_info("[PPO] Loading model automatically: %s (Python=%s)\n",bundle,python);
  int64_t deadline=now_ms()+60000;
  while(!stopping && now_ms()<deadline) {
    int status;
    pid_t r=waitpid(python_pid,&status,WNOHANG);
    if(r==python_pid) {
      python_pid=0;
      ppo_error("[PPO] Python exited before ready; see its error above. No E2 policy sent.\n");
      return false;
    }
    struct stat st;
    if(stat(socket_path,&st)==0 && S_ISSOCK(st.st_mode)) return true;
    usleep(100000);
  }
  ppo_error("[PPO] Model startup cancelled/timed out; no E2 policy sent.\n");
  return false;
}

int main(int argc, char **argv)
{
  setvbuf(stdout, NULL, _IOLBF, 0);
  if (!open_ppo_log()) { fprintf(stderr, "[PPO] Cannot create log file; no control started.\n"); return 1; }
  if (!read_pair("VA_RNTIS", &target[0]) || !read_pair("NONVA_RNTIS", &target[2])) return 1;
  for (unsigned i=0;i<4;i++)
    for (unsigned j=0;j<i;j++)
      if (target[i]==target[j]) { ppo_error("Four distinct RNTIs required\n"); return 1; }
  unsigned minimum=5;
  const char *radio_mode = getenv("PPO_RADIO_INPUT");
  if (radio_mode && strcmp(radio_mode, "0") && strcmp(radio_mode, "1")) {
    ppo_error("PPO_RADIO_INPUT must be 0 (legacy PPO4) or 1 (radio PPO4R)\n");
    return 1;
  }
  radio_inputs = radio_mode && !strcmp(radio_mode, "1");
  ppo_info("[PPO] Input protocol=%s; radio PRB denominator=6 UL slots/10ms\n",
          radio_inputs ? "PPO4R" : "PPO4");
  const char *m = getenv("PPO_MIN_PRBS");
  if (m) {
    char *end=NULL;
    errno=0;
    unsigned long v=strtoul(m, &end, 10);
    if (errno || end==m || *end || v<1 || v>136) {
      ppo_error("PPO_MIN_PRBS must be 1..136\n"); return 1;
    }
    minimum=v;
  }
  signal(SIGINT,stop); signal(SIGTERM,stop);
  char bundle[PATH_MAX];
  if(!find_bundle(bundle)) {
    ppo_error("Cannot find xapp_basic_no_lending. Place it beside openairinterface5g or set PPO_BUNDLE.\n");
    return 1;
  }
  char directory[]="/tmp/oai-ppo-client-XXXXXX";
  if (!mkdtemp(directory)) { perror("mkdtemp"); return 1; }
  struct sockaddr_un local={.sun_family=AF_UNIX}, server={.sun_family=AF_UNIX};
  snprintf(local.sun_path, sizeof(local.sun_path), "%s/client.sock", directory);
  snprintf(server.sun_path,sizeof(server.sun_path),"%s/server.sock",directory);
  snprintf(owned_directory,sizeof(owned_directory),"%s",directory);
  snprintf(owned_server,sizeof(owned_server),"%s",server.sun_path);
  snprintf(owned_client,sizeof(owned_client),"%s",local.sun_path);
  if(atexit(cleanup_python)!=0) { rmdir(directory); return 1; }
  int fd=socket(AF_UNIX, SOCK_DGRAM, 0);
  if (fd<0 || bind(fd, (struct sockaddr *)&local, sizeof(local))<0) {
    perror("local socket");
    if(fd>=0) close(fd);
    rmdir(directory); return 1;
  }
  if(!start_python(bundle,server.sun_path,minimum,fd)) { close(fd); return 1; }
  fr_args_t args=init_fr_args(argc, argv);
  init_xapp_api(&args);
  /* FlexRIC replaces signal handlers during initialization. */
  struct sigaction sa={0};
  sa.sa_handler=stop;
  sigemptyset(&sa.sa_mask);
  sigaddset(&sa.sa_mask, SIGINT); sigaddset(&sa.sa_mask, SIGTERM);
  if (sigaction(SIGINT, &sa, NULL) || sigaction(SIGTERM, &sa, NULL)) {
    perror("sigaction"); _exit(1);
  }
  sleep(1);
  e2_node_arr_xapp_t nodes=e2_nodes_xapp_api();
  sm_ans_xapp_t slice_sub={0}, mac_sub={0};
  bool touched=false;
  int result=1;
  if (nodes.len != 1) { ppo_error("Exactly one E2 node required\n"); goto done; }
  bool has_mac=false, has_slice=false;
  for(size_t i=0;i<nodes.n[0].len_rf;i++) {
    has_mac |= nodes.n[0].rf[i].id==142;
    has_slice |= nodes.n[0].rf[i].id==145;
  }
  if (!has_mac || !has_slice) goto done;
  slice_sub=report_sm_xapp_api(&nodes.n[0].id, 145, "10_ms", on_report);
  if (!slice_sub.success) goto done;
  mac_sub=report_sm_xapp_api(&nodes.n[0].id, 142, "10_ms", on_mac);
  if (!mac_sub.success) goto done;
  int64_t deadline=now_ms()+10000;
  bool ready=false;
  while (!stopping && now_ms()<deadline) {
    pthread_mutex_lock(&lock);
    bool existing=state.reports && (state.slice_count || state.any_assoc);
    ready=state.reports && state.ue_count==4 && state.found[0] && state.found[1] && state.found[2] && state.found[3];
    pthread_mutex_unlock(&lock);
    if(existing) { ppo_error("Stop the other controller first: policy already exists\n"); goto done; }
    if(ready) break;
    usleep(10000);
  }
  if(!ready || stopping) {
    pthread_mutex_lock(&lock);
    ppo_error("[PPO] START_NOT_READY reports=%" PRIu64 " UEs=%u age_ms=%lld\n",
              state.reports,state.ue_count,state.received_ms ? (long long)(now_ms()-state.received_ms) : -1LL);
    for(unsigned k=0;k<4;k++)
      ppo_error("[PPO] expected=%04x found=%d service=%u reported_rnti[%u]=%04x\n",
                target[k],state.found[k],state.assoc[k],k,state.observed[k]);
    pthread_mutex_unlock(&lock);
    goto done;
  }
  /* Bootstrap before traffic: old large HARQ grants may cause ASSOC rejection. */
  touched=true;
  uint64_t before=report_count();
  if(!quotas(&nodes.n[0].id,137,136) || !confirm(before,137,136,false,false)) goto done;
  before=report_count();
  if(!associate(&nodes.n[0].id) || !confirm(before,137,136,true,false)) goto done;
  ppo_line("[PPO] READY: 4 UEs; 14 radio/budget values sent to Python; latency bundle adds 4 service measurements (18 actor inputs). total=273, stored=137:136, tick=100ms, inference deadline=50ms.");
  ppo_line("[PPO] Mapping: VA_RNTIS first/second = VA screen UE1/UE2; NONVA_RNTIS first/second = NonVA UE1/UE2. Start both service servers, even for idle services.");
  ppo_line("[PPO] Input TP is MAC RX throughput; 80..120ms actual windows accepted. This is not latency QoS validation.");
  telemetry_t prev={0};
  unsigned confirmed[2]={137,136};
  uint64_t seq=0;
  int64_t last_hold_log=0;
  int64_t next=now_ms()+100;
  result=0;
  while(!stopping) {
    int64_t now=now_ms();
    if(now<next) { usleep((useconds_t)((next-now)*1000)); continue; }
    int64_t tick_start=now_ms();
    next+=100;
    if(python_pid>0) {
      int status;
      if(waitpid(python_pid,&status,WNOHANG)==python_pid) {
        python_pid=0;
        ppo_line("[PPO] Python stopped; retaining last budget until xApp exits. No further inference.");
      }
    }
    telemetry_t cur;
    unsigned actual[2];
    pthread_mutex_lock(&lock);
    cur=telemetry;
    actual[0]=state.prbs[0]; actual[1]=state.prbs[1];
    bool healthy=state.received_ms>0 && now_ms()-state.received_ms<=200
                 && actual[0]>=minimum && actual[1]>=minimum && actual[0]+actual[1]==273
                 && matches(actual[0],actual[1],true);
    if(!healthy && (last_hold_log==0 || now_ms()-last_hold_log>=1000)) {
      int64_t age=state.received_ms ? now_ms()-state.received_ms : -1;
      ppo_info("[PPO] HOLD membership/budget report age_ms=%lld UEs=%u quotas=%u:%u\n",
               (long long)age,state.ue_count,actual[0],actual[1]);
      for(unsigned k=0;k<4;k++)
        ppo_info("[PPO] target=%04x found=%d service=%u expected_service=%u\n",
                 target[k],state.found[k],state.assoc[k],k/2+1);
      last_hold_log=now_ms();
    }
    pthread_mutex_unlock(&lock);
    unsigned proposal[2];
    if(healthy) {
      last_hold_log=0;
      /* Only observed stored budgets enter the model, never last proposed values. */
      confirmed[0]=actual[0]; confirmed[1]=actual[1];
      if(!infer(fd,&server,++seq,&cur,&prev,confirmed,minimum,proposal)) {
        ppo_line("[PPO] HOLD: warm-up/stale input, invalid output, or Python unavailable/late");
      } else if(proposal[0]==confirmed[0] && proposal[1]==confirmed[1]) {
        ppo_info("[PPO] unchanged stored=%u:%u (no duplicate Control)\n",confirmed[0],confirmed[1]);
      } else {
        pthread_mutex_lock(&lock);
        bool still_current=now_ms()-state.received_ms<=200
                           && matches(confirmed[0],confirmed[1],true);
        pthread_mutex_unlock(&lock);
        if(!still_current || stopping) {
          ppo_line("[PPO] HOLD: membership/budget changed during inference");
          prev=cur;
          if(now_ms()>=next) next=now_ms()+100;
          continue;
        }
        before=report_count();
        int64_t sent=now_ms();
        bool ack=quotas(&nodes.n[0].id,proposal[0],proposal[1]);
        bool stored=false;
        int64_t until=sent+80;
        while(ack && !stopping && now_ms()<until) {
          pthread_mutex_lock(&lock);
          stored=state.reports>before && now_ms()-state.received_ms<=200
                 && matches(proposal[0],proposal[1],true);
          pthread_mutex_unlock(&lock);
          if(stored) break;
          usleep(1000);
        }
        if(stored) {
          confirmed[0]=proposal[0]; confirmed[1]=proposal[1];
          ppo_info("[PPO] STORED=%u:%u confirm_ms=%lld; MAC application requires E2_SLICE_APPLIED\n",
                 confirmed[0],confirmed[1],(long long)(now_ms()-sent));
        } else {
          ppo_line("[PPO] NOT_CONFIRMED: do not assume applied; next tick re-reads Agent state. Check E2_SLICE_REJECT.");
        }
      }
    }
    prev=cur;
    int64_t finished=now_ms();
    if(finished>=next) {
      ppo_info("[PPO] DEADLINE_MISS tick_ms=%lld; skipping overdue ticks\n",(long long)(finished-tick_start));
      next=finished+100;
    }
  }
done:
  /* Print a usable path even if policy cleanup below blocks/aborts. Each
   * message is already flushed, and the file stays open for cleanup results. */
  ppo_info("[PPO] Stopping; log file: %s\n", ppo_log_path);
  cleanup_python();
  /* Normal exit restores the shared total-273 baseline. */
  if(touched) {
    uint64_t before_cleanup=report_count();
    if(!remove_pair(&nodes.n[0].id) || !confirm(before_cleanup,0,0,false,true)) {
      ppo_error("Cleanup not confirmed; policy may remain. Restart OAI if necessary.\n");
      result=1;
    } else ppo_line("[PPO] Quotas removed; shared total-273 baseline restored.");
  }
  stopping=1;
  close_ppo_log(); /* before unsubscribe, which may abort on a FlexRIC timeout */
  if (log_failed) result=1;
  if(mac_sub.success) rm_report_sm_xapp_api(mac_sub.u.handle);
  if(slice_sub.success) rm_report_sm_xapp_api(slice_sub.u.handle);
  while(!try_stop_xapp_api()) usleep(1000);
  free_e2_node_arr_xapp(&nodes);
  close(fd); unlink(local.sun_path); rmdir(directory);
  return result;
}

