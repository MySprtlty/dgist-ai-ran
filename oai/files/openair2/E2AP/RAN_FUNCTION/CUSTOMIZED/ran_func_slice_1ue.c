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

/* Experimental NR contract: one MAC instance, one UE, UL slice ID 1.
 * STATIC positions are inclusive BWP-relative PRBs, pos_low must be 0.
 * Slice 0 means unassociated. No DL or S-NSSAI mapping. */
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
  if (mac->e2_ul_slice_prbs) {
    ind->msg.slice_conf.ul.len_slices = 1;
    fr_slice_t *s = calloc(1, sizeof(*s));
    AssertFatal(s != NULL, "Slice report allocation failed\n");
    ind->msg.slice_conf.ul.slices = s;
    s->id = 1;
    s->params.type = SLICE_ALG_SM_V0_STATIC;
    s->params.u.sta.pos_high = mac->e2_ul_slice_prbs - 1;
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
          .rnti = ue->rnti, .ul_id = ue->e2_ul_slice_associated && mac->e2_ul_slice_prbs ? 1 : 0};
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

sm_ag_if_ans_t write_ctrl_slice_sm(void const *data)
{
  gNB_MAC_INST *mac = slice_mac();
  if (!data || !mac)
    return outcome(false, "ERROR: one NR MAC instance required");
  const slice_ctrl_msg_t *msg = &((const slice_ctrl_req_data_t *)data)->msg;
  const char *reason = NULL;
  NR_SCHED_LOCK(&mac->sched_lock);
  NR_UE_info_t *ue = mac->UE_info.connected_ue_list[0];
  /* Delete also works without a UE, to restore unrestricted scheduling. */
  if (msg->type == SLICE_CTRL_SM_V0_DEL) {
    const del_slice_conf_t *d = &msg->u.del_slice;
    if (d->len_dl || d->len_ul != 1 || !d->ul || d->ul[0] != 1) {
      reason = "ERROR: only delete UL slice 1 is supported";
    } else {
      mac->e2_ul_slice_prbs = 0;
      UE_iterator(mac->UE_info.connected_ue_list, member) {
        member->e2_ul_slice_associated = false;
      }
    }
  } else if (!ue || mac->UE_info.connected_ue_list[1]) {
    reason = "ERROR: exactly one connected UE required";
  } else if (msg->type == SLICE_CTRL_SM_V0_ADD) {
    const slice_conf_t *c = &msg->u.add_mod_slice;
    if (c->dl.len_slices || c->dl.len_sched_name || c->ul.len_slices != 1 || !c->ul.slices
        || !pf_name(c->ul.len_sched_name, c->ul.sched_name)) {
      reason = "ERROR: only one UL STATIC slice with PF and no DL settings is supported";
    } else {
      const fr_slice_t *s = c->ul.slices;
      const bwp_info_t bwp = get_pusch_bwp_start_size(ue);
      if (s->id != 1 || s->params.type != SLICE_ALG_SM_V0_STATIC || !pf_name(s->len_sched, s->sched))
        reason = "ERROR: expected slice ID 1, STATIC, PF";
      else if (s->params.u.sta.pos_low != 0 || s->params.u.sta.pos_high >= 275 || s->params.u.sta.pos_high + 1 < mac->min_grant_prb
               || bwp.bwpSize <= 0 || s->params.u.sta.pos_high >= (uint32_t)bwp.bwpSize)
        reason = "ERROR: window must start at 0, meet min_grant_prb, and fit UE BWP";
      else
        mac->e2_ul_slice_prbs = s->params.u.sta.pos_high + 1;
    }
  } else if (msg->type == SLICE_CTRL_SM_V0_UE_SLICE_ASSOC) {
    const ue_slice_conf_t *a = &msg->u.ue_slice;
    if (a->len_ue_slice != 1 || !a->ues || a->ues[0].rnti != ue->rnti || a->ues[0].dl_id != 0
        || a->ues[0].ul_id > 1 || (a->ues[0].ul_id == 1 && !mac->e2_ul_slice_prbs))
      reason = "ERROR: association requires current RNTI and existing UL slice 1 (0 detaches)";
    else
      ue->e2_ul_slice_associated = a->ues[0].ul_id == 1;
  } else {
    reason = "ERROR: unsupported Slice Control type";
  }
  if (!reason) {
    mac->e2_ul_slice_revision++;
    LOG_I(NR_MAC, "[E2_SLICE_ACCEPT] direction=UL revision=%u type=%d prbs=%u rnti=%04x associated=%d\n",
          mac->e2_ul_slice_revision, msg->type, (unsigned)mac->e2_ul_slice_prbs,
          ue ? ue->rnti : 0, ue ? ue->e2_ul_slice_associated : 0);
  } else {
    LOG_W(NR_MAC, "[E2_SLICE_REJECT] %s; previous policy unchanged\n", reason);
  }
  NR_SCHED_UNLOCK(&mac->sched_lock);
  return outcome(reason == NULL, reason ? reason : "OK: stored; MAC application is logged separately");
}

