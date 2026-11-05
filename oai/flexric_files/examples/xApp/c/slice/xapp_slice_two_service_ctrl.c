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
#include <time.h>
#include <unistd.h>

/* Explicit current RNTIs, not list order or AMF UE IDs. This does not classify
 * application traffic or map S-NSSAI. One UE per experimental slice, same BWP.
 * Pair windows are updated in one ADD message. No borrowing/retransmission cap. */
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static volatile sig_atomic_t stopping;
static uint16_t target[2];
static struct {
  uint64_t reports;
  int64_t received_ms;
  unsigned ue_count, slice_count;
  bool found[2];
  uint32_t assoc[2];
  unsigned start[2], prbs[2];
} state;

static int64_t now_ms(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void stop(int sig) { (void)sig; stopping = 1; }

static void on_report(sm_ag_if_rd_t const *rd)
{
  if (!rd || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != SLICE_STATS_V0)
    return;
  const slice_ind_msg_t *m = &rd->ind.slice.msg;
  pthread_mutex_lock(&lock);
  state.reports++;
  state.received_ms = now_ms();
  state.ue_count = m->ue_slice_conf.len_ue_slice;
  state.slice_count = m->slice_conf.ul.len_slices;
  for (unsigned k = 0; k < 2; k++) {
    state.found[k] = false;
    state.assoc[k] = state.start[k] = state.prbs[k] = 0;
  }
  for (unsigned i = 0; i < m->ue_slice_conf.len_ue_slice; i++) {
    const ue_slice_assoc_t *u = &m->ue_slice_conf.ues[i];
    for (unsigned k = 0; k < 2; k++)
      if (u->rnti == target[k]) {
        state.found[k] = true;
        state.assoc[k] = u->ul_id;
      }
  }
  for (unsigned i = 0; i < m->slice_conf.ul.len_slices; i++) {
    const fr_slice_t *s = &m->slice_conf.ul.slices[i];
    if (s->id >= 1 && s->id <= 2 && s->params.type == SLICE_ALG_SM_V0_STATIC
        && s->params.u.sta.pos_high >= s->params.u.sta.pos_low) {
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
  return state.ue_count == 2 && state.found[0] && state.found[1]
         && state.slice_count == 2 && state.start[0] == 0 && state.start[1] == first
         && state.prbs[0] == first && state.prbs[1] == second
         && state.assoc[0] == (associated ? 1u : 0u)
         && state.assoc[1] == (associated ? 2u : 0u);
}

static bool confirm(uint64_t after, unsigned first, unsigned second, bool associated, bool cleanup)
{
  int64_t deadline = now_ms() + 5000;
  while ((cleanup || !stopping) && now_ms() < deadline) {
    pthread_mutex_lock(&lock);
    bool ok = state.reports > after && (cleanup ? state.slice_count == 0
                                               : matches(first, second, associated));
    pthread_mutex_unlock(&lock);
    if (ok) return true;
    usleep(10000);
  }
  fprintf(stderr, "[SLICE_XAPP] Confirmation timeout; check RIC and E2_SLICE_REJECT.\n");
  return false;
}

static bool windows(global_e2_node_id_t *id, unsigned first, unsigned second)
{
  fr_slice_t slices[2] = {0};
  for (unsigned k = 0; k < 2; k++) {
    slices[k].id = k + 1;
    slices[k].params.type = SLICE_ALG_SM_V0_STATIC;
  }
  slices[0].params.u.sta.pos_high = first - 1;
  slices[1].params.u.sta.pos_low = first;
  slices[1].params.u.sta.pos_high = first + second - 1;
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_ADD;
  req.msg.u.add_mod_slice.ul.len_slices = 2;
  req.msg.u.add_mod_slice.ul.slices = slices;
  printf("[SLICE_XAPP] send_ms=%lld ADD_PAIR s1=[0,%u] s2=[%u,%u] prbs=%u:%u\n",
         (long long)now_ms(), first - 1, first, first + second - 1, first, second);
  return control_sm_xapp_api(id, SM_SLICE_ID, &req).success;
}

static bool associate(global_e2_node_id_t *id)
{
  ue_slice_assoc_t members[2] = {{.rnti = target[0], .ul_id = 1}, {.rnti = target[1], .ul_id = 2}};
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_UE_SLICE_ASSOC;
  req.msg.u.ue_slice.len_ue_slice = 2;
  req.msg.u.ue_slice.ues = members;
  printf("[SLICE_XAPP] ASSOC_PAIR service1_rnti=%04x service2_rnti=%04x\n", target[0], target[1]);
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

static bool read_rnti(const char *name, uint16_t *value)
{
  const char *env = getenv(name);
  char *end = NULL;
  errno = 0;
  unsigned long n = env ? strtoul(env, &end, 16) : 0;
  if (!env || end == env || *end || errno || n == 0 || n >= 0xfff0) {
    fprintf(stderr, "%s must be the current UE C-RNTI in hexadecimal (e.g. 1234), not an AMF UE ID.\n", name);
    return false;
  }
  *value = n;
  return true;
}

int main(int argc, char **argv)
{
  if (!read_rnti("SLICE1_RNTI", &target[0]) || !read_rnti("SLICE2_RNTI", &target[1]) || target[0] == target[1])
    return 1;
  const unsigned policy[][2] = {{55, 218}, {164, 109}, {218, 55}};
  const unsigned hold_ms = 20000;
  setvbuf(stdout, NULL, _IOLBF, 0);
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
    fprintf(stderr, "Exactly one E2 node required. No Control sent.\n");
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
    bool existing = state.reports && (state.slice_count || state.assoc[0] || state.assoc[1]);
    ready = state.reports && state.ue_count == 2 && state.found[0] && state.found[1];
    pthread_mutex_unlock(&lock);
    if (existing) {
      fprintf(stderr, "Existing policy detected; refusing to overwrite. No Control sent.\n");
      goto done;
    }
    if (ready) break;
    usleep(10000);
  }
  if (!ready || stopping) {
    fprintf(stderr, "Two connected UEs matching SLICE1_RNTI/SLICE2_RNTI required. No Control sent.\n");
    goto done;
  }
  for (unsigned stage = 0; stage < 3; stage++) {
    if (stopping) goto done;
    uint64_t before = report_count();
    touched = true;
    if (!windows(&nodes.n[0].id, policy[stage][0], policy[stage][1])
        || !confirm(before, policy[stage][0], policy[stage][1], stage != 0, false)) goto done;
    if (stage == 0) {
      before = report_count();
      if (!associate(&nodes.n[0].id) || !confirm(before, policy[stage][0], policy[stage][1], true, false)) goto done;
    }
    printf("[SLICE_XAPP] STORED stage=%u service1=%u service2=%u hold_ms=%u; verify per-UE E2_SLICE_APPLIED\n",
           stage + 1, policy[stage][0], policy[stage][1], hold_ms);
    deadline = now_ms() + hold_ms;
    while (!stopping && now_ms() < deadline) {
      pthread_mutex_lock(&lock);
      bool healthy = matches(policy[stage][0], policy[stage][1], true) && now_ms() - state.received_ms < 2000;
      pthread_mutex_unlock(&lock);
      if (!healthy) {
        fprintf(stderr, "UE membership/policy changed or reports stopped; ending experiment.\n");
        goto done;
      }
      usleep(10000);
    }
  }
  result = stopping ? 1 : 0;
done:
  if (touched) {
    uint64_t before = report_count();
    if (!remove_pair(&nodes.n[0].id) || !confirm(before, 0, 0, false, true)) {
      fprintf(stderr, "RESTORE NOT CONFIRMED. Restart OAI if policy remains active.\n");
      result = 1;
    } else printf("[SLICE_XAPP] both slices deleted; unrestricted new UL scheduling restored\n");
  }
  if (subscribed) rm_report_sm_xapp_api(sub.u.handle);
  while (!try_stop_xapp_api()) usleep(1000);
  free_e2_node_arr_xapp(&nodes);
  return result;
}
