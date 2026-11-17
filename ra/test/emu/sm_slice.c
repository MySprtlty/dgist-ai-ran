/*
 * SPDX-License-Identifier: LicenseRef-CSSL-1.0
 *
 * ICNL 11/11, TEST ONLY (IITP demo, Resource Allocator test): FlexRIC's emulated gNB with a Slice SM that remembers what it
 * is told, like the lab's OAI gNB (count-quota contract NR_UL_PRB_COUNT_V1: label, pos_low 0, pos_high + 1 = PRB count;
 * services 1 = VA, 2 = Non-VA): ADD sets the quotas, UE_SLICE_ASSOC sets the UEs' services, DEL clears both. The report
 * carries the stored quotas and the UEs of EMU_RNTIS with their service, as the lab's read_slice_sm does. The state, plus
 * the UE PRB floors that sm_rc.c receives (emu_state_set_floor), is written to EMU_STATE_FILE (JSON) for the test tools.
 * Without EMU_RNTIS the stock random report is used.
 */
#include "sm_slice.h"
#include "../../../test/rnd/fill_rnd_data_slice.h"
#include "../../../src/util/time_now_us.h"
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EMU_LABEL "NR_UL_PRB_COUNT_V1"
#define EMU_MAX 16
static pthread_mutex_t emu_mtx = PTHREAD_MUTEX_INITIALIZER;
static unsigned emu_quota[2];
static struct {
  uint16_t rnti;
  uint32_t ul;
  int floor;
  int64_t until_us;
} emu_ue[EMU_MAX];
static int emu_n = -1;

static void emu_init_ues(void) /* under emu_mtx */
{
  if (emu_n >= 0)
    return;
  emu_n = 0;
  const char* env = getenv("EMU_RNTIS");
  if (env == NULL)
    return;
  char buf[160];
  snprintf(buf, sizeof(buf), "%s", env);
  for (char* t = strtok(buf, ","); t != NULL && emu_n < EMU_MAX; t = strtok(NULL, ","))
    emu_ue[emu_n++].rnti = (uint16_t)strtoul(t, NULL, 16);
}

static void emu_write_state(void) /* under emu_mtx */
{
  const char* path = getenv("EMU_STATE_FILE");
  if (path == NULL)
    return;
  char tmp[512];
  snprintf(tmp, sizeof(tmp), "%s.tmp", path);
  FILE* f = fopen(tmp, "w");
  if (f == NULL)
    return;
  fprintf(f, "{\"t_us\":%lld,\"quota\":{\"va\":%u,\"nonva\":%u},\"ues\":[", (long long)time_now_us(), emu_quota[0], emu_quota[1]);
  for (int i = 0; i < emu_n; i++)
    fprintf(f, "%s{\"rnti\":\"%04x\",\"ul\":%u,\"floor\":%d,\"until_us\":%lld}", i ? "," : "", emu_ue[i].rnti, emu_ue[i].ul,
            emu_ue[i].floor, (long long)emu_ue[i].until_us);
  fprintf(f, "]}\n");
  fclose(f);
  rename(tmp, path);
}

/* called by sm_rc.c for RC Style 2 action 101 parameter 5 */
void emu_state_set_floor(uint16_t rnti, int floor, int validity_ms);
void emu_state_set_floor(uint16_t rnti, int floor, int validity_ms)
{
  pthread_mutex_lock(&emu_mtx);
  emu_init_ues();
  for (int i = 0; i < emu_n; i++)
    if (emu_ue[i].rnti == rnti) {
      emu_ue[i].floor = floor;
      emu_ue[i].until_us = validity_ms > 0 ? time_now_us() + (int64_t)validity_ms * 1000 : 0;
    }
  emu_write_state();
  pthread_mutex_unlock(&emu_mtx);
}

void init_slice_sm(void)
{
  // No allocation needed
}

void free_slice_sm(void)
{
  // No allocation needed
}

bool read_slice_sm(void* data)
{
  assert(data != NULL);
  slice_ind_data_t* slice = (slice_ind_data_t*)data;
  pthread_mutex_lock(&emu_mtx);
  emu_init_ues();
  if (emu_n == 0) {
    pthread_mutex_unlock(&emu_mtx);
    fill_slice_ind_data(slice);
    return true;
  }
  *slice = (slice_ind_data_t){0};
  slice->msg.tstamp = time_now_us();
  for (unsigned k = 0; k < 2; k++)
    slice->msg.slice_conf.ul.len_slices += emu_quota[k] != 0;
  if (slice->msg.slice_conf.ul.len_slices) {
    fr_slice_t* s = calloc(slice->msg.slice_conf.ul.len_slices, sizeof(*s));
    assert(s != NULL);
    slice->msg.slice_conf.ul.slices = s;
    for (unsigned k = 0; k < 2; k++) {
      if (!emu_quota[k])
        continue;
      s->id = k + 1;
      s->params.type = SLICE_ALG_SM_V0_STATIC;
      s->params.u.sta.pos_low = 0;
      s->params.u.sta.pos_high = emu_quota[k] - 1;
      s->label = strdup(EMU_LABEL);
      s->len_label = strlen(EMU_LABEL);
      s++;
    }
  }
  slice->msg.ue_slice_conf.len_ue_slice = emu_n;
  slice->msg.ue_slice_conf.ues = calloc(emu_n, sizeof(ue_slice_assoc_t));
  assert(slice->msg.ue_slice_conf.ues != NULL);
  for (int i = 0; i < emu_n; i++)
    slice->msg.ue_slice_conf.ues[i] = (ue_slice_assoc_t){.rnti = emu_ue[i].rnti, .ul_id = emu_ue[i].ul};
  pthread_mutex_unlock(&emu_mtx);
  return true;
}

void read_slice_setup_sm(void* data)
{
  assert(data != NULL);
  assert(0 != 0 && "Not supported");
}

sm_ag_if_ans_t write_ctrl_slice_sm(void const* data)
{
  assert(data != NULL);
  slice_ctrl_req_data_t const* slice_req_ctrl = (slice_ctrl_req_data_t const*)data;
  slice_ctrl_msg_t const* msg = &slice_req_ctrl->msg;
  pthread_mutex_lock(&emu_mtx);
  emu_init_ues();
  if (msg->type == SLICE_CTRL_SM_V0_ADD) {
    const ul_dl_slice_conf_t* ul = &msg->u.add_mod_slice.ul;
    for (uint32_t i = 0; i < ul->len_slices; i++) {
      const fr_slice_t* s = &ul->slices[i];
      if (s->id >= 1 && s->id <= 2 && s->params.type == SLICE_ALG_SM_V0_STATIC && s->params.u.sta.pos_low == 0 && s->label
          && s->len_label == strlen(EMU_LABEL) && !memcmp(s->label, EMU_LABEL, s->len_label))
        emu_quota[s->id - 1] = s->params.u.sta.pos_high + 1;
    }
    printf("[E2 Agent]: SLICE CONTROL ADD rx -> quotas %u:%u\n", emu_quota[0], emu_quota[1]);
  } else if (msg->type == SLICE_CTRL_SM_V0_DEL) {
    emu_quota[0] = emu_quota[1] = 0;
    for (int i = 0; i < emu_n; i++)
      emu_ue[i].ul = 0;
    printf("[E2 Agent]: SLICE CONTROL DEL rx\n");
  } else if (msg->type == SLICE_CTRL_SM_V0_UE_SLICE_ASSOC) {
    const ue_slice_conf_t* a = &msg->u.ue_slice;
    for (uint32_t j = 0; j < a->len_ue_slice; j++)
      for (int i = 0; i < emu_n; i++)
        if (emu_ue[i].rnti == a->ues[j].rnti)
          emu_ue[i].ul = a->ues[j].ul_id;
    printf("[E2 Agent]: SLICE CONTROL ASSOC rx (%u UEs)\n", a->len_ue_slice);
  } else {
    assert(0 != 0 && "Unknown msg_type!");
  }
  emu_write_state();
  pthread_mutex_unlock(&emu_mtx);
  sm_ag_if_ans_t ans = {.type = CTRL_OUTCOME_SM_AG_IF_ANS_V0};
  ans.ctrl_out.type = SLICE_AGENT_IF_CTRL_ANS_V0;
  return ans;
}
