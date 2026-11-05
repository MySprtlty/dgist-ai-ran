/*
 * Licensed to the OpenAirInterface (OAI) Software Alliance under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The OpenAirInterface Software Alliance licenses this file to You under
 * the OAI Public License, Version 1.1  (the "License"); you may not use this file
 * except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.openairinterface.org/?page_id=698
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *-------------------------------------------------------------------------------
 * For more information about the OpenAirInterface (OAI) Software Alliance:
 *      contact@openairinterface.org
 */

#include "ran_func_slice.h"
#include "common/ran_context.h"
#include "openair2/LAYER2/NR_MAC_gNB/mac_proto.h"
#include "../../flexric/src/util/time_now_us.h"
#include <stdlib.h>
#include <string.h>

/* Local versioned count-quota contract, NOT standard STATIC frequency slicing.
 * The existing Slice SM wire layout is retained: label identifies the contract,
 * pos_low MUST be zero, pos_high+1 encodes a PRB COUNT, never a position.
 * Two service IDs (1/2), exactly one UE each, total 78, no lending. */
static gNB_MAC_INST *slice_mac(void)
{
  return RC.nb_nr_macrlc_inst == 1 && RC.nrmac ? RC.nrmac[0] : NULL;
}

static bool pf_name(uint32_t len, const char *name)
{
  return len == 0 || (len == 2 && name && memcmp(name, "PF", 2) == 0);
}

static sm_ag_if_ans_t outcome(bool ok, const char *reason)
{
  sm_ag_if_ans_t ans = {.type = CTRL_OUTCOME_SM_AG_IF_ANS_V0};
  ans.ctrl_out.type = SLICE_AGENT_IF_CTRL_ANS_V0;
  ans.ctrl_out.slice.ans = ok ? SLICE_CTRL_OUT_OK : SLICE_CTRL_OUT_ERROR;
  /* Existing PLAIN codec transmits diagnostic, not the ans enum.
   * Include status in text; xApp confirms stored state via indications. */
  ans.ctrl_out.slice.len_diag = strlen(reason);
  ans.ctrl_out.slice.diagnostic = strdup(reason);
  AssertFatal(ans.ctrl_out.slice.diagnostic != NULL, "Slice diagnostic allocation failed\n");
  return ans;
}

bool read_slice_sm(void *data)
{
  gNB_MAC_INST *mac = slice_mac();
  if (!data || !mac)
    return false;
  slice_ind_data_t *ind = data;
  *ind = (slice_ind_data_t){0};
  NR_SCHED_LOCK(&mac->sched_lock);
  ind->msg.tstamp = time_now_us();
  for (unsigned k = 0; k < 2; k++)
    ind->msg.slice_conf.ul.len_slices += mac->e2_ul_slice_prbs[k] != 0;
  if (ind->msg.slice_conf.ul.len_slices) {
    fr_slice_t *s = calloc(ind->msg.slice_conf.ul.len_slices, sizeof(*s));
    AssertFatal(s != NULL, "Slice report allocation failed\n");
    ind->msg.slice_conf.ul.slices = s;
    for (unsigned k = 0; k < 2; k++) {
      if (!mac->e2_ul_slice_prbs[k])
        continue;
      s->id = k + 1;
      s->params.type = SLICE_ALG_SM_V0_STATIC;
      s->params.u.sta.pos_low = 0;
      s->params.u.sta.pos_high = mac->e2_ul_slice_prbs[k] - 1;
      s->label = strdup(NR_UL_PRB_QUOTA_LABEL);
      AssertFatal(s->label != NULL, "Quota label allocation failed\n");
      s->len_label = strlen(NR_UL_PRB_QUOTA_LABEL);
      s++;
    }
  }
  UE_iterator(mac->UE_info.connected_ue_list, ue) {
    ind->msg.ue_slice_conf.len_ue_slice++;
  }
  size_t n = ind->msg.ue_slice_conf.len_ue_slice;
  if (n) {
    ind->msg.ue_slice_conf.ues = calloc(n, sizeof(ue_slice_assoc_t));
    AssertFatal(ind->msg.ue_slice_conf.ues != NULL, "UE slice report allocation failed\n");
    size_t i = 0;
    UE_iterator(mac->UE_info.connected_ue_list, ue) {
      ind->msg.ue_slice_conf.ues[i++] = (ue_slice_assoc_t){
          .rnti = ue->rnti, .ul_id = ue->e2_ul_slice_id};
    }
  }
  NR_SCHED_UNLOCK(&mac->sched_lock);
  return true;
}

void read_slice_setup_sm(void *data)
{
  /* Current plugin advertises its name directly rather than calling here. */
  slice_e2_setup_data_t *setup = data;
  if (!setup)
    return;
  *setup = (slice_e2_setup_data_t){0};
  setup->func_def.len_supported_alg = 1;
  setup->func_def.supported_alg = calloc(1, sizeof(slice_algorithm_e));
  AssertFatal(setup->func_def.supported_alg != NULL, "Slice setup allocation failed\n");
  setup->func_def.supported_alg[0] = SLICE_ALG_SM_V0_STATIC;
}

/* Commit only a fully validated pair. Partial updates/deletes are rejected. */
sm_ag_if_ans_t write_ctrl_slice_sm(void const *data)
{
  gNB_MAC_INST *mac = slice_mac();
  if (!data || !mac)
    return outcome(false, "ERROR: one NR MAC instance required");
  const slice_ctrl_msg_t *msg = &((const slice_ctrl_req_data_t *)data)->msg;
  const char *reason = NULL;
  NR_SCHED_LOCK(&mac->sched_lock);
  uint16_t count[2];
  memcpy(count, mac->e2_ul_slice_prbs, sizeof(count));
  NR_UE_info_t *targets[2] = {0};
  uint8_t membership[2] = {0};
  unsigned n = 0;
  UE_iterator(mac->UE_info.connected_ue_list, ue) {
    if (n < 2) {
      targets[n] = ue;
      membership[n] = ue->e2_ul_slice_id;
    }
    n++;
  }
  bool deleting = msg->type == SLICE_CTRL_SM_V0_DEL;
  bool seen[2] = {0};
  if (deleting) {
    const del_slice_conf_t *d = &msg->u.del_slice;
    if (d->len_dl || d->len_ul != 2 || !d->ul
        || !((d->ul[0] == 1 && d->ul[1] == 2) || (d->ul[0] == 2 && d->ul[1] == 1)))
      reason = "ERROR: delete both UL quota IDs 1 and 2 together";
    else
      count[0] = count[1] = 0;
  } else if (n != 2) {
    reason = "ERROR: count-quota experiment requires exactly two connected UEs";
  } else if (msg->type == SLICE_CTRL_SM_V0_ADD) {
    const slice_conf_t *c = &msg->u.add_mod_slice;
    if (c->dl.len_slices || c->dl.len_sched_name || c->ul.len_slices != 2 || !c->ul.slices
        || !pf_name(c->ul.len_sched_name, c->ul.sched_name)) {
      reason = "ERROR: both UL count quotas are required, with PF and no DL settings";
    } else {
      for (unsigned i = 0; i < 2; i++) {
        const fr_slice_t *s = &c->ul.slices[i];
        if (s->id < 1 || s->id > 2 || seen[s->id - 1]
            || s->params.type != SLICE_ALG_SM_V0_STATIC || !pf_name(s->len_sched, s->sched)
            || s->len_label != strlen(NR_UL_PRB_QUOTA_LABEL) || !s->label
            || memcmp(s->label, NR_UL_PRB_QUOTA_LABEL, strlen(NR_UL_PRB_QUOTA_LABEL))) {
          reason = "ERROR: expected distinct IDs 1/2 and NR_UL_PRB_COUNT_V1 label; legacy frequency commands rejected";
          break;
        }
        seen[s->id - 1] = true;
        uint32_t hi = s->params.u.sta.pos_high;
        if (s->params.u.sta.pos_low != 0 || hi >= NR_UL_PRB_TOTAL_BUDGET
            || hi + 1 < mac->min_grant_prb) {
          reason = "ERROR: count must fit total budget and min_grant_prb; pos_low must be zero";
          break;
        }
        count[s->id - 1] = hi + 1;
      }
      if (!reason && count[0] + count[1] != NR_UL_PRB_TOTAL_BUDGET)
        reason = "ERROR: service quotas must sum to 78";
    }
  } else if (msg->type == SLICE_CTRL_SM_V0_UE_SLICE_ASSOC) {
    const ue_slice_conf_t *a = &msg->u.ue_slice;
    if (a->len_ue_slice != 2 || !a->ues || !count[0] || !count[1]) {
      reason = "ERROR: associate both UEs after installing both quotas";
    } else {
      for (unsigned i = 0; i < 2; i++) {
        const ue_slice_assoc_t *e = &a->ues[i];
        unsigned j = targets[0]->rnti == e->rnti ? 0 : targets[1]->rnti == e->rnti ? 1 : 2;
        if (j == 2 || seen[j] || e->dl_id || e->ul_id < 1 || e->ul_id > 2) {
          reason = "ERROR: invalid/duplicate current RNTI or service ID";
          break;
        }
        seen[j] = true;
        membership[j] = e->ul_id;
      }
      if (!reason && membership[0] == membership[1])
        reason = "ERROR: one UE per service required";
    }
  } else {
    reason = "ERROR: unsupported Slice Control type";
  }
  if (!reason && !deleting) {
    bwp_info_t first = get_pusch_bwp_start_size(targets[0]);
    for (unsigned j = 0; j < 2 && !reason; j++) {
      bwp_info_t bwp = get_pusch_bwp_start_size(targets[j]);
      if (bwp.bwpSize < NR_UL_PRB_TOTAL_BUDGET || bwp.bwpStart != first.bwpStart || bwp.bwpSize != first.bwpSize) {
        reason = "ERROR: identical UL BWPs with at least 78 PRBs required";
        break;
      }
      /* Avoid accepting a reduction that strands an in-flight/pending HARQ
       * TB which cannot fit its service's strict per-slot budget. Retry later. */
      if (membership[j] >= 1 && membership[j] <= 2) {
        for (unsigned h = 0; h < NR_MAX_HARQ_PROCESSES; h++) {
          const NR_UE_ul_harq_t *harq = &targets[j]->UE_sched_ctrl.ul_harq_processes[h];
          if ((harq->is_waiting || harq->round > 0) && harq->sched_pusch.rbSize > count[membership[j] - 1]) {
            reason = "ERROR: pending HARQ grant exceeds quota; retry after it completes";
            break;
          }
        }
      }
    }
  }
  if (!reason) {
    memcpy(mac->e2_ul_slice_prbs, count, sizeof(count));
    if (deleting) {
      UE_iterator(mac->UE_info.connected_ue_list, ue) {
        ue->e2_ul_slice_id = 0;
      }
    } else {
      for (unsigned j = 0; j < 2; j++)
        targets[j]->e2_ul_slice_id = membership[j];
    }
    mac->e2_ul_slice_revision++;
    LOG_I(NR_MAC, "[E2_SLICE_ACCEPT] mode=count_no_lending revision=%u type=%d total=78 s1_prbs=%u s2_prbs=%u\n",
          mac->e2_ul_slice_revision, msg->type, (unsigned)count[0], (unsigned)count[1]);
    UE_iterator(mac->UE_info.connected_ue_list, ue) {
      LOG_I(NR_MAC, "[E2_SLICE_ASSOC] revision=%u rnti=%04x slice=%u\n",
            mac->e2_ul_slice_revision, ue->rnti, (unsigned)ue->e2_ul_slice_id);
    }
  } else {
    LOG_W(NR_MAC, "[E2_SLICE_REJECT] %s; previous policy unchanged\n", reason);
  }
  NR_SCHED_UNLOCK(&mac->sched_lock);
  return outcome(reason == NULL, reason ? reason : "OK: stored count quotas; verify MAC application separately");
}
