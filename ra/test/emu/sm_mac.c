/*
 * SPDX-License-Identifier: LicenseRef-CSSL-1.0
 *
 * ICNL 11/11, TEST ONLY (IITP demo): FlexRIC's emulated gNB with a MAC SM report the tests can steer.
 *   EMU_SIM_FILE=path  the report comes from that JSON file, rewritten by the UL simulator (ra/tools/va_sim.py):
 *                      {"ues":[{"rnti":"4601","tbs":<cumulative UL bytes>,"prb":<cumulative new-tx PRBs>,"rprb":0,"mcs":20,
 *                               "bsr":<bytes>,"snr":20.0,"bler":0.0}]}
 *   EMU_RNTIS=a,b,c,d  (no sim file, or not readable yet) exactly these UEs with growing counters (the PPO xApp test)
 *   neither            the stock random report
 */
#include "sm_mac.h"
#include "../../../test/rnd/fill_rnd_data_mac.h"
#include "../../../src/util/time_now_us.h"
#include "ra_slicing_link.h" /* the small JSON reader (copied next to this file for the test build) */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void init_mac_sm(void)
{
  // No allocation needed
}

void free_mac_sm(void)
{
  // No allocation needed
}

static bool fill_from_sim(mac_ind_data_t* mac)
{
  const char* path = getenv("EMU_SIM_FILE");
  if (path == NULL || path[0] == 0)
    return false;
  FILE* f = fopen(path, "r");
  if (f == NULL)
    return false;
  char buf[8192];
  const size_t len = fread(buf, 1, sizeof(buf) - 1, f);
  fclose(f);
  buf[len] = 0;
  const char* e = buf + len;
  const char* o = buf;
  while (o < e && *o != '{') o++;
  const char* a = o < e ? rsl__get(o, e, "ues") : NULL;
  if (a == NULL || *a != '[')
    return false;
  const char* end = rsl__skip(a, e);
  if (end == NULL)
    return false;
  mac_ue_stats_impl_t tmp[16];
  int n = 0;
  for (const char* p = a + 1; p && p < end && n < 16;) {
    while (p < end && (*p == ' ' || *p == ',' || *p == '\n')) p++;
    if (p >= end || *p != '{') break;
    char r[8];
    if (rsl__str(p, e, "rnti", r, sizeof(r))) {
      mac_ue_stats_impl_t* u = &tmp[n++];
      memset(u, 0, sizeof(*u));
      u->rnti = (uint32_t)strtoul(r, NULL, 16);
      u->ul_aggr_tbs = rsl__u64(p, e, "tbs", 0);
      u->ul_aggr_prb = (uint32_t)rsl__u64(p, e, "prb", 0);
      u->ul_aggr_retx_prb = (uint32_t)rsl__u64(p, e, "rprb", 0);
      u->ul_mcs1 = (uint8_t)rsl__num(p, e, "mcs", 0);
      u->bsr = (uint32_t)rsl__num(p, e, "bsr", 0);
      u->pusch_snr = (float)rsl__num(p, e, "snr", 20.0);
      u->ul_bler = (float)rsl__num(p, e, "bler", 0.0);
      u->pucch_snr = 20.0f;
      u->wb_cqi = 12;
      u->ul_num_harq = u->dl_num_harq = 4;
    }
    p = rsl__skip(p, e);
  }
  mac_ind_msg_t* m = &mac->msg;
  m->tstamp = time_now_us();
  m->len_ue_stats = n;
  m->ue_stats = n ? calloc(n, sizeof(mac_ue_stats_impl_t)) : NULL;
  if (n)
    memcpy(m->ue_stats, tmp, n * sizeof(mac_ue_stats_impl_t));
  return true;
}

static bool fill_fixed_ues(mac_ind_data_t* mac)
{
  static uint32_t rn[8];
  static int n = -1;
  static uint64_t tick;
  const char* env = getenv("EMU_RNTIS");
  if (env == NULL || env[0] == 0)
    return false;
  if (n < 0) {
    char buf[128];
    snprintf(buf, sizeof(buf), "%s", env);
    n = 0;
    for (char* t = strtok(buf, ","); t != NULL && n < 8; t = strtok(NULL, ","))
      rn[n++] = (uint32_t)strtoul(t, NULL, 16);
  }
  tick++;
  mac_ind_msg_t* m = &mac->msg;
  m->tstamp = time_now_us();
  m->len_ue_stats = n;
  m->ue_stats = calloc(n, sizeof(mac_ue_stats_impl_t));
  assert(m->ue_stats != NULL);
  for (int i = 0; i < n; i++) {
    mac_ue_stats_impl_t* u = &m->ue_stats[i];
    u->rnti = rn[i];
    u->ul_aggr_tbs = tick * (1000 + 100 * i);
    u->dl_aggr_tbs = tick * 500;
    u->ul_aggr_prb = tick * (10 + i);
    u->ul_aggr_retx_prb = tick;
    u->pusch_snr = 20.0f + i;
    u->pucch_snr = 20.0f;
    u->bsr = 1000 * (i + 1);
    u->ul_mcs1 = u->dl_mcs1 = 20;
    u->wb_cqi = 12;
    u->ul_num_harq = u->dl_num_harq = 4;
  }
  return true;
}

bool read_mac_sm(void* data)
{
  assert(data != NULL);
  mac_ind_data_t* mac = (mac_ind_data_t*)data;
  if (!fill_from_sim(mac) && !fill_fixed_ues(mac))
    fill_mac_ind_data(mac);
  return true;
}

void read_mac_setup_sm(void* data)
{
  assert(data != NULL);
  assert(0 != 0 && "Not supported");
}

sm_ag_if_ans_t write_ctrl_mac_sm(void const* data)
{
  assert(data != NULL);
  mac_ctrl_req_data_t* ctrl = (mac_ctrl_req_data_t*)data;
  assert(ctrl->hdr.dummy == 1);
  assert(ctrl->msg.action == 42);
  sm_ag_if_ans_t ans = {.type = CTRL_OUTCOME_SM_AG_IF_ANS_V0};
  return ans;
}
