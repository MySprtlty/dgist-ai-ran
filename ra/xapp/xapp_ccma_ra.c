/*
 * xapp_ccma_ra.c (ICNL, 2026-11-11) -- the E2 side of the Resource Allocator (RA) of the IITP 6G demo.
 *
 * The RA is two processes: this FlexRIC xApp (E2 in and out) and ra_main.py (the decisions). They talk over local UDP, one
 * JSON object per datagram:
 *   events  (this xApp -> ra_main.py, RA_SHELL_EVT, default 127.0.0.1:9823)
 *     hello  {node, rf[], has_mac, has_slice, has_rc, rc_floors}
 *     mac    {t_us, ues[{rnti, bsr, snr, mcs, bler, phr, tbs, prb, rprb}]}   every MAC SM report (RA_MAC_PERIOD, 10_ms)
 *     slice  {t_us, quota[{id, prb, label}], ues[{rnti, ul}]}               every Slice SM report (RA_SLICE_PERIOD, 10_ms)
 *     ack    {id, cmd, ok, rtt_us, reason}                                    one per command
 *   commands (ra_main.py -> this xApp, RA_SHELL_CMD, default 127.0.0.1:9824)
 *     quota  {id, va, nonva}        Slice SM ADD, the lab's count-quota contract NR_UL_PRB_COUNT_V1 (as xapp_slice_ppo_ctrl)
 *     assoc  {id, ues[{rnti, ul}]}  Slice SM UE_SLICE_ASSOC
 *     del    {id}                   Slice SM DEL of services 1 and 2
 *     floor  {id, rnti, prb, validity_ms}  E2SM-RC Control Style 2 action 101 "CCMA UL link adaptation" with parameter 5
 *                                   "UL PRB floor per slot" (ICNL gNB patch). Sent only with RA_RC_FLOORS=1: a gNB without
 *                                   the ICNL ran_func_rc.c patch asserts on an RC control it does not know.
 *     quit   {}
 *
 *   xapp_ccma_ra [-c flexric.conf] [-p sm_dir]
 * Exactly one E2 node is used (the first). Stops on SIGINT / SIGTERM / quit.
 */
#include "xApp/e42_xapp_api.h"
#include "sm/mac_sm/mac_sm_id.h"
#include "sm/rc_sm/ie/ir/ran_param_list.h"
#include "sm/rc_sm/ie/ir/ran_param_struct.h"
#include "sm/rc_sm/rc_sm_id.h"
#include "sm/slice_sm/slice_sm_id.h"
#include "util/time_now_us.h"

#include "ra_slicing_link.h" /* UDP host:port parsing and the small JSON reader */

#include <assert.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define QUOTA_LABEL "NR_UL_PRB_COUNT_V1"
enum { CCMA_RC_ACT_UL_LA = 101 };
enum { P_RNTI = 1, P_MCS_CAP = 2, P_WEIGHT = 3, P_VALIDITY = 4, P_FLOOR = 5 };

static volatile sig_atomic_t stopping;
static int evt_fd = -1;
static struct sockaddr_in evt_addr;
static bool rc_floors;
static global_e2_node_id_t node;
static uint64_t n_mac, n_slice;

static void on_signal(int s)
{
  (void)s;
  if (stopping) _exit(130);
  stopping = 1;
}

static void emit(const char* buf, int len)
{
  if (len > 0 && len <= RSL_MAX_DGRAM)
    (void)sendto(evt_fd, buf, (size_t)len, 0, (const struct sockaddr*)&evt_addr, sizeof(evt_addr));
}

/* ------------------------------------------------------------------------------------------------- E2 reports */
static void on_mac(sm_ag_if_rd_t const* rd)
{
  if (!rd || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != MAC_STATS_V0) return;
  const mac_ind_msg_t* m = &rd->ind.mac.msg;
  char buf[RSL_MAX_DGRAM + 1];
  int n = snprintf(buf, sizeof(buf), "{\"type\":\"mac\",\"v\":1,\"t_us\":%lld,\"rx_us\":%lld,\"ues\":[", (long long)m->tstamp,
                   (long long)time_now_us());
  for (uint32_t i = 0; i < m->len_ue_stats && n < (int)sizeof(buf) - 200; i++) {
    const mac_ue_stats_impl_t* u = &m->ue_stats[i];
    n += snprintf(buf + n, sizeof(buf) - (size_t)n,
                  "%s{\"rnti\":\"%04x\",\"bsr\":%u,\"snr\":%.1f,\"mcs\":%u,\"bler\":%.4f,\"phr\":%d,\"tbs\":%llu,\"prb\":%u,\"rprb\":%u}",
                  i ? "," : "", (unsigned)u->rnti, (unsigned)u->bsr, (double)u->pusch_snr, (unsigned)u->ul_mcs1,
                  (double)u->ul_bler, (int)u->phr, (unsigned long long)u->ul_aggr_tbs, (unsigned)u->ul_aggr_prb,
                  (unsigned)u->ul_aggr_retx_prb);
  }
  n += snprintf(buf + n, sizeof(buf) - (size_t)n, "]}");
  emit(buf, n);
  n_mac++;
}

static void on_slice(sm_ag_if_rd_t const* rd)
{
  if (!rd || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != SLICE_STATS_V0) return;
  const slice_ind_msg_t* m = &rd->ind.slice.msg;
  char buf[RSL_MAX_DGRAM + 1];
  int n = snprintf(buf, sizeof(buf), "{\"type\":\"slice\",\"v\":1,\"t_us\":%lld,\"quota\":[", (long long)m->tstamp);
  for (uint32_t i = 0; i < m->slice_conf.ul.len_slices && n < (int)sizeof(buf) - 120; i++) {
    const fr_slice_t* s = &m->slice_conf.ul.slices[i];
    char label[40] = {0};
    if (s->label && s->len_label) snprintf(label, sizeof(label), "%.*s", (int)(s->len_label < 39 ? s->len_label : 39), s->label);
    const unsigned prb = s->params.type == SLICE_ALG_SM_V0_STATIC ? s->params.u.sta.pos_high - s->params.u.sta.pos_low + 1 : 0;
    n += snprintf(buf + n, sizeof(buf) - (size_t)n, "%s{\"id\":%u,\"prb\":%u,\"low\":%u,\"label\":\"%s\"}", i ? "," : "",
                  (unsigned)s->id, prb, s->params.type == SLICE_ALG_SM_V0_STATIC ? (unsigned)s->params.u.sta.pos_low : 0u, label);
  }
  n += snprintf(buf + n, sizeof(buf) - (size_t)n, "],\"ues\":[");
  for (uint32_t i = 0; i < m->ue_slice_conf.len_ue_slice && n < (int)sizeof(buf) - 40; i++) {
    const ue_slice_assoc_t* u = &m->ue_slice_conf.ues[i];
    n += snprintf(buf + n, sizeof(buf) - (size_t)n, "%s{\"rnti\":\"%04x\",\"ul\":%u}", i ? "," : "", (unsigned)u->rnti, (unsigned)u->ul_id);
  }
  n += snprintf(buf + n, sizeof(buf) - (size_t)n, "]}");
  emit(buf, n);
  n_slice++;
}

/* ----------------------------------------------------------------------------------------------- Slice SM control */
static bool slice_quotas(unsigned va, unsigned nonva)
{
  fr_slice_t slices[2] = {0};
  const unsigned prb[2] = {va, nonva};
  for (unsigned k = 0; k < 2; k++) {
    slices[k].id = k + 1;
    slices[k].params.type = SLICE_ALG_SM_V0_STATIC;
    slices[k].params.u.sta.pos_low = 0;
    slices[k].params.u.sta.pos_high = prb[k] - 1;
    slices[k].label = QUOTA_LABEL;
    slices[k].len_label = strlen(QUOTA_LABEL);
  }
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_ADD;
  req.msg.u.add_mod_slice.ul.len_slices = 2;
  req.msg.u.add_mod_slice.ul.slices = slices;
  return control_sm_xapp_api(&node, SM_SLICE_ID, &req).success;
}

static bool slice_assoc(const ue_slice_assoc_t* ues, unsigned n)
{
  ue_slice_assoc_t members[RSL_MAX_UES] = {0};
  for (unsigned i = 0; i < n && i < RSL_MAX_UES; i++) members[i] = ues[i];
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_UE_SLICE_ASSOC;
  req.msg.u.ue_slice.len_ue_slice = n;
  req.msg.u.ue_slice.ues = members;
  return control_sm_xapp_api(&node, SM_SLICE_ID, &req).success;
}

static bool slice_delete(void)
{
  uint32_t ids[2] = {1, 2};
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_DEL;
  req.msg.u.del_slice.len_ul = 2;
  req.msg.u.del_slice.ul = ids;
  return control_sm_xapp_api(&node, SM_SLICE_ID, &req).success;
}

/* -------------------------------------------------------------------------------------- RC Style 2 (UE PRB floor) */
static seq_ran_param_t int_param(uint32_t id, int64_t v)
{
  seq_ran_param_t p = {0};
  p.ran_param_id = id;
  p.ran_param_val.type = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  p.ran_param_val.flag_false = calloc(1, sizeof(ran_parameter_value_t));
  assert(p.ran_param_val.flag_false != NULL);
  p.ran_param_val.flag_false->type = INTEGER_RAN_PARAMETER_VALUE;
  p.ran_param_val.flag_false->int_ran = v;
  return p;
}

/* RC control header Format 1, Style 2. Format 1 requires a UE ID; our action names the UE by its C-RNTI (parameter 1), so a
 * placeholder gNB UE ID goes here (as the 10/28 CCMA xApp, spark_realue/flexric/xapp_ccma/xapp_ccma_ctrl.c). */
static bool rc_floor(uint16_t rnti, int floor_prb, int validity_ms)
{
  rc_ctrl_req_data_t c = {0};
  c.hdr.format = FORMAT_1_E2SM_RC_CTRL_HDR;
  c.hdr.frmt_1.ue_id.type = GNB_UE_ID_E2SM;
  c.hdr.frmt_1.ue_id.gnb.amf_ue_ngap_id = 0;
  c.hdr.frmt_1.ue_id.gnb.guami.plmn_id = (e2sm_plmn_t){.mcc = 999, .mnc = 70, .mnc_digit_len = 2};
  c.hdr.frmt_1.ric_style_type = 2;
  c.hdr.frmt_1.ctrl_act_id = CCMA_RC_ACT_UL_LA;
  c.msg.format = FORMAT_1_E2SM_RC_CTRL_MSG;
  c.msg.frmt_1.sz_ran_param = 5;
  c.msg.frmt_1.ran_param = calloc(5, sizeof(seq_ran_param_t));
  assert(c.msg.frmt_1.ran_param != NULL);
  c.msg.frmt_1.ran_param[0] = int_param(P_RNTI, rnti);
  c.msg.frmt_1.ran_param[1] = int_param(P_MCS_CAP, -1);      /* no MCS cap */
  c.msg.frmt_1.ran_param[2] = int_param(P_WEIGHT, 0);        /* keep the PF weight */
  c.msg.frmt_1.ran_param[3] = int_param(P_VALIDITY, validity_ms);
  c.msg.frmt_1.ran_param[4] = int_param(P_FLOOR, floor_prb);
  const bool ok = control_sm_xapp_api(&node, SM_RC_ID, &c).success;
  free_rc_ctrl_req_data(&c);
  return ok;
}

/* ------------------------------------------------------------------------------------------------------ commands */
static void ack(unsigned long long id, const char* cmd, bool ok, long long rtt_us, const char* reason)
{
  char buf[256];
  const int n = snprintf(buf, sizeof(buf), "{\"type\":\"ack\",\"v\":1,\"id\":%llu,\"cmd\":\"%s\",\"ok\":%s,\"rtt_us\":%lld,\"reason\":\"%s\"}",
                         id, cmd, ok ? "true" : "false", rtt_us, reason ? reason : "");
  emit(buf, n);
}

static void handle(const char* buf, size_t len)
{
  const char* e = buf + len;
  const char* o = buf;
  while (o < e && (*o == ' ' || *o == '\n')) o++;
  if (o >= e || *o != '{' || !rsl__skip(o, e)) return;
  char type[16];
  if (!rsl__str(o, e, "type", type, sizeof(type))) return;
  const unsigned long long id = rsl__u64(o, e, "id", 0);
  const long long t0 = time_now_us();
  if (!strcmp(type, "quota")) {
    const int va = (int)rsl__num(o, e, "va", 0), nonva = (int)rsl__num(o, e, "nonva", 0);
    if (va < 1 || nonva < 1) { ack(id, type, false, 0, "va and nonva must be >= 1"); return; }
    const bool ok = slice_quotas((unsigned)va, (unsigned)nonva);
    printf("[RA-E2] %lld quota %d:%d -> %s\n", t0, va, nonva, ok ? "ok" : "FAILED");
    ack(id, type, ok, time_now_us() - t0, ok ? "" : "E2 control failed");
  } else if (!strcmp(type, "assoc")) {
    ue_slice_assoc_t ues[RSL_MAX_UES];
    unsigned n = 0;
    const char* a = rsl__get(o, e, "ues");
    if (a && *a == '[') {
      const char* end = rsl__skip(a, e);
      const char* p = a + 1;
      while (p && end && p < end && n < RSL_MAX_UES) {
        while (p < end && (*p == ' ' || *p == ',')) p++;
        if (p >= end || *p != '{') break;
        char r[8];
        if (rsl__str(p, e, "rnti", r, sizeof(r)))
          ues[n++] = (ue_slice_assoc_t){.rnti = (uint16_t)strtoul(r, NULL, 16), .ul_id = (uint32_t)rsl__num(p, e, "ul", 0), .dl_id = 0};
        p = rsl__skip(p, e);
      }
    }
    if (n == 0) { ack(id, type, false, 0, "no UEs"); return; }
    const bool ok = slice_assoc(ues, n);
    printf("[RA-E2] %lld assoc %u UEs -> %s\n", t0, n, ok ? "ok" : "FAILED");
    ack(id, type, ok, time_now_us() - t0, ok ? "" : "E2 control failed");
  } else if (!strcmp(type, "del")) {
    const bool ok = slice_delete();
    printf("[RA-E2] %lld delete services 1, 2 -> %s\n", t0, ok ? "ok" : "FAILED");
    ack(id, type, ok, time_now_us() - t0, ok ? "" : "E2 control failed");
  } else if (!strcmp(type, "floor")) {
    char r[8];
    const int prb = (int)rsl__num(o, e, "prb", -1), val = (int)rsl__num(o, e, "validity_ms", 0);
    if (!rsl__str(o, e, "rnti", r, sizeof(r)) || prb < 0) { ack(id, type, false, 0, "rnti and prb >= 0 required"); return; }
    if (!rc_floors) { ack(id, type, false, 0, "RC floors disabled (RA_RC_FLOORS=1, needs the gNB UL PRB floor patch in oai/)"); return; }
    const bool ok = rc_floor((uint16_t)strtoul(r, NULL, 16), prb, val);
    ack(id, type, ok, time_now_us() - t0, ok ? "" : "E2 control failed");
  } else if (!strcmp(type, "quit")) {
    stopping = 1;
  } else {
    ack(id, type, false, 0, "unknown command");
  }
  fflush(stdout);
}

int main(int argc, char* argv[])
{
  setvbuf(stdout, NULL, _IOLBF, 0);
  const char* evt = getenv("RA_SHELL_EVT") ? getenv("RA_SHELL_EVT") : "127.0.0.1:9823";
  const char* cmd = getenv("RA_SHELL_CMD") ? getenv("RA_SHELL_CMD") : "127.0.0.1:9824";
  const char* mac_period = getenv("RA_MAC_PERIOD") ? getenv("RA_MAC_PERIOD") : "10_ms";
  const char* slice_period = getenv("RA_SLICE_PERIOD") ? getenv("RA_SLICE_PERIOD") : "10_ms";
  rc_floors = getenv("RA_RC_FLOORS") && !strcmp(getenv("RA_RC_FLOORS"), "1");
  rsl_link_t cmd_link; /* bound to RA_SHELL_CMD; its peer is the event address */
  if (rsl_open(&cmd_link, cmd, evt) != 0) { fprintf(stderr, "[RA-E2] cannot bind %s: %s\n", cmd, strerror(errno)); return 1; }
  evt_fd = cmd_link.fd;
  evt_addr = cmd_link.peer;

  fr_args_t args = init_fr_args(argc, argv);
  init_xapp_api(&args);
  struct sigaction sa = {0}; /* FlexRIC replaces the signal handlers during init */
  sa.sa_handler = on_signal;
  sigemptyset(&sa.sa_mask);
  sigaction(SIGINT, &sa, NULL);
  sigaction(SIGTERM, &sa, NULL);
  sleep(1);
  e2_node_arr_xapp_t nodes = e2_nodes_xapp_api();
  if (nodes.len == 0) { fprintf(stderr, "[RA-E2] no E2 node\n"); return 1; }
  node = nodes.n[0].id;
  bool has_mac = false, has_slice = false, has_rc = false;
  char rf[256];
  int n = 0;
  rf[0] = 0;
  for (size_t j = 0; j < nodes.n[0].len_rf && n < (int)sizeof(rf) - 8; j++) {
    const int id = nodes.n[0].rf[j].id;
    has_mac |= id == SM_MAC_ID;
    has_slice |= id == SM_SLICE_ID;
    has_rc |= id == SM_RC_ID;
    n += snprintf(rf + n, sizeof(rf) - (size_t)n, "%s%d", j ? "," : "", id);
  }
  printf("[RA-E2] E2 nodes %d, using nb_id %u (RAN functions %s); events -> %s, commands <- %s, RC floors %s\n", (int)nodes.len,
         (unsigned)node.nb_id.nb_id, rf, evt, cmd, rc_floors ? "ON" : "off");
  char hello[512];
  n = snprintf(hello, sizeof(hello),
               "{\"type\":\"hello\",\"v\":1,\"node\":{\"nb_id\":%u,\"mcc\":%u,\"mnc\":%u},\"rf\":[%s],\"has_mac\":%s,\"has_slice\":%s,"
               "\"has_rc\":%s,\"rc_floors\":%s}",
               (unsigned)node.nb_id.nb_id, (unsigned)node.plmn.mcc, (unsigned)node.plmn.mnc, rf, has_mac ? "true" : "false",
               has_slice ? "true" : "false", has_rc ? "true" : "false", rc_floors ? "true" : "false");
  emit(hello, n);
  if (!has_mac || !has_slice) { fprintf(stderr, "[RA-E2] the E2 node needs the MAC (142) and Slice (145) SMs\n"); return 1; }
  if (rc_floors && !has_rc) { fprintf(stderr, "[RA-E2] RA_RC_FLOORS=1 but the E2 node has no RC SM (3)\n"); return 1; }
  sm_ans_xapp_t mac_h = report_sm_xapp_api(&node, SM_MAC_ID, (void*)mac_period, on_mac);
  sm_ans_xapp_t slice_h = report_sm_xapp_api(&node, SM_SLICE_ID, (void*)slice_period, on_slice);
  if (!mac_h.success || !slice_h.success) { fprintf(stderr, "[RA-E2] subscription failed (MAC %d Slice %d)\n", mac_h.success, slice_h.success); return 1; }
  printf("[RA-E2] subscribed MAC %s, Slice %s; ready\n", mac_period, slice_period);
  emit(hello, n); /* again, now that the reports flow */

  char buf[RSL_MAX_DGRAM + 1];
  while (!stopping) {
    struct pollfd p = {.fd = cmd_link.fd, .events = POLLIN};
    if (poll(&p, 1, 100) <= 0) continue;
    struct sockaddr_in from;
    socklen_t fl = sizeof(from);
    const ssize_t r = recvfrom(cmd_link.fd, buf, RSL_MAX_DGRAM, 0, (struct sockaddr*)&from, &fl);
    if (r <= 0 || from.sin_addr.s_addr != evt_addr.sin_addr.s_addr) continue;
    buf[r] = 0;
    handle(buf, (size_t)r);
  }
  printf("[RA-E2] stopping after %llu MAC and %llu Slice reports\n", (unsigned long long)n_mac, (unsigned long long)n_slice);
  rm_report_sm_xapp_api(mac_h.u.handle);
  rm_report_sm_xapp_api(slice_h.u.handle);
  while (try_stop_xapp_api() == false) usleep(1000);
  free_e2_node_arr_xapp(&nodes);
  rsl_close(&cmd_link);
  return 0;
}
