/* SPDX-License-Identifier: LicenseRef-CSSL-1.0 */
#include "../../../../src/xApp/e42_xapp_api.h"
#include "../../../../src/sm/slice_sm/slice_sm_id.h"
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

/* Keep the existing slice and KPM examples unchanged. This experiment accepts
 * exactly one E2 node and one real UE, and confirms state by Slice reports. */
static pthread_mutex_t state_lock = PTHREAD_MUTEX_INITIALIZER;
static struct {
  unsigned reports;
  unsigned ue_count;
  uint16_t rnti;
  uint32_t slice_id;
  unsigned prbs;
} state;
static volatile sig_atomic_t stopping;

static void on_signal(int sig) { (void)sig; stopping = 1; }

static int64_t monotonic_ms(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void on_report(sm_ag_if_rd_t const *rd)
{
  if (!rd || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != SLICE_STATS_V0)
    return;
  const slice_ind_msg_t *m = &rd->ind.slice.msg;
  pthread_mutex_lock(&state_lock);
  state.reports++;
  state.ue_count = m->ue_slice_conf.len_ue_slice;
  state.rnti = state.ue_count == 1 ? m->ue_slice_conf.ues[0].rnti : 0;
  state.slice_id = state.ue_count == 1 ? m->ue_slice_conf.ues[0].ul_id : 0;
  state.prbs = 0;
  if (m->slice_conf.ul.len_slices == 1) {
    const fr_slice_t *s = m->slice_conf.ul.slices;
    if (s->id == 1 && s->params.type == SLICE_ALG_SM_V0_STATIC && s->params.u.sta.pos_low == 0)
      state.prbs = s->params.u.sta.pos_high + 1;
  }
  pthread_mutex_unlock(&state_lock);
}

static unsigned report_count(void)
{
  pthread_mutex_lock(&state_lock);
  unsigned n = state.reports;
  pthread_mutex_unlock(&state_lock);
  return n;
}

/* A transport success is not proof of policy acceptance or MAC application.
 * Require a subsequent indication matching the expected stored state. */
static bool wait_state(unsigned after, unsigned prbs, int assoc, uint16_t rnti, bool cleanup)
{
  int64_t deadline = monotonic_ms() + 5000;
  while ((cleanup || !stopping) && monotonic_ms() < deadline) {
    pthread_mutex_lock(&state_lock);
    bool ok = state.reports > after && state.prbs == prbs
              && (cleanup || (state.ue_count == 1 && state.rnti == rnti
                              && (assoc < 0 || state.slice_id == (unsigned)assoc)));
    pthread_mutex_unlock(&state_lock);
    if (ok)
      return true;
    usleep(10000);
  }
  fprintf(stderr, "[SLICE_XAPP] State confirmation timeout; inspect E2_SLICE_REJECT and agent connectivity.\n");
  return false;
}

static bool send_window(global_e2_node_id_t *id, unsigned prbs)
{
  fr_slice_t slice = {.id = 1};
  slice.params.type = SLICE_ALG_SM_V0_STATIC;
  slice.params.u.sta.pos_low = 0;
  slice.params.u.sta.pos_high = prbs - 1;
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_ADD;
  req.msg.u.add_mod_slice.ul.len_slices = 1;
  req.msg.u.add_mod_slice.ul.slices = &slice;
  printf("[SLICE_XAPP] send_ms=%lld ADD direction=UL slice=1 window=[0,%u]\n",
         (long long)monotonic_ms(), prbs - 1);
  return control_sm_xapp_api(id, SM_SLICE_ID, &req).success;
}

static bool associate(global_e2_node_id_t *id, uint16_t rnti)
{
  ue_slice_assoc_t assoc = {.rnti = rnti, .ul_id = 1};
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_UE_SLICE_ASSOC;
  req.msg.u.ue_slice.len_ue_slice = 1;
  req.msg.u.ue_slice.ues = &assoc;
  printf("[SLICE_XAPP] ASSOC direction=UL rnti=%04x slice=1\n", rnti);
  return control_sm_xapp_api(id, SM_SLICE_ID, &req).success;
}

static bool remove_slice(global_e2_node_id_t *id)
{
  uint32_t slice_id = 1;
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_DEL;
  req.msg.u.del_slice.len_ul = 1;
  req.msg.u.del_slice.ul = &slice_id;
  printf("[SLICE_XAPP] DELETE slice=1; restore unrestricted new UL grants\n");
  return control_sm_xapp_api(id, SM_SLICE_ID, &req).success;
}

int main(int argc, char **argv)
{
  /* Environment variable avoids interfering with FlexRIC's own CLI parser. */
  unsigned hold_ms = 20000;
  const char *env = getenv("SLICE_HOLD_MS");
  if (env) {
    char *end = NULL;
    errno = 0;
    unsigned long n = strtoul(env, &end, 10);
    if (errno || end == env || *end || n < 100 || n > 3600000) {
      fprintf(stderr, "SLICE_HOLD_MS must be 100..3600000\n");
      return 1;
    }
    hold_ms = n;
  }
  fr_args_t args = init_fr_args(argc, argv);
  init_xapp_api(&args);
  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);
  sleep(1);
  e2_node_arr_xapp_t nodes = e2_nodes_xapp_api();
  int result = 1;
  bool subscribed = false, touched = false;
  sm_ans_xapp_t sub = {0};
  if (nodes.len != 1) {
    fprintf(stderr, "Expected exactly one E2 node; got %u. No Control sent.\n", (unsigned)nodes.len);
    goto done;
  }
  bool supported = false;
  for (size_t i = 0; i < nodes.n[0].len_rf; i++)
    supported |= nodes.n[0].rf[i].id == SM_SLICE_ID;
  if (!supported) {
    fprintf(stderr, "Node does not advertise Slice SM. No Control sent.\n");
    goto done;
  }
  /* Existing Slice SM parser supports 1/2/5/10 ms, not KPM's period field. */
  sub = report_sm_xapp_api(&nodes.n[0].id, SM_SLICE_ID, "10_ms", on_report);
  if (!sub.success)
    goto done;
  subscribed = true;
  int64_t deadline = monotonic_ms() + 10000;
  uint16_t rnti = 0;
  while (!stopping && monotonic_ms() < deadline) {
    pthread_mutex_lock(&state_lock);
    if (state.reports && state.ue_count == 1)
      rnti = state.rnti;
    unsigned existing_prbs = state.prbs;
    pthread_mutex_unlock(&state_lock);
    if (existing_prbs) {
      fprintf(stderr, "Existing slice policy detected; refusing to overwrite it. No Control sent.\n");
      goto done;
    }
    if (rnti)
      break;
    usleep(10000);
  }
  if (!rnti || stopping) {
    fprintf(stderr, "Exactly one connected UE is required. No Control sent.\n");
    goto done;
  }
  printf("[SLICE_XAPP] target rnti=%04x hold_ms=%u\n", rnti, hold_ms);
  const unsigned windows[] = {200, 100, 50};
  for (size_t i = 0; i < sizeof(windows) / sizeof(windows[0]); i++) {
    if (stopping)
      goto done;
    unsigned before = report_count();
    touched = true;
    if (!send_window(&nodes.n[0].id, windows[i])
        || !wait_state(before, windows[i], i == 0 ? 0 : 1, rnti, false))
      goto done;
    if (i == 0) {
      before = report_count();
      if (!associate(&nodes.n[0].id, rnti) || !wait_state(before, windows[i], 1, rnti, false))
        goto done;
    }
    printf("[SLICE_XAPP] STORED direction=UL prbs=%u; verify E2_SLICE_APPLIED in MAC log, and KPM throughput.\n", windows[i]);
    deadline = monotonic_ms() + hold_ms;
    while (!stopping && monotonic_ms() < deadline)
      usleep(10000);
  }
  result = stopping ? 1 : 0;
done:
  if (touched) {
    unsigned before = report_count();
    if (!remove_slice(&nodes.n[0].id) || !wait_state(before, 0, -1, 0, true)) {
      fprintf(stderr, "RESTORE NOT CONFIRMED. Policy may remain active; restart OAI to clear it.\n");
      result = 1;
    } else {
      printf("[SLICE_XAPP] unrestricted policy confirmed\n");
    }
  }
  if (subscribed)
    rm_report_sm_xapp_api(sub.u.handle);
  while (!try_stop_xapp_api())
    usleep(1000);
  free_e2_node_arr_xapp(&nodes);
  return result;
}

