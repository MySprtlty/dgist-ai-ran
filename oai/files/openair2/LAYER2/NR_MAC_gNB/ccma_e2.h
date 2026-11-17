/*
 * ICNL 2026-11-11 (IITP 6G demo, Resource Allocator): UL PRB floor per slot and UE from the Resource Allocator xApp over
 * E2SM-RC Control Style 2 action 101 parameter 5, handled in openair2/E2AP/RAN_FUNCTION/O-RAN/ran_func_rc.c and applied
 * in gNB_scheduler_ulsch.c (pf_ul). Called from the E2 agent thread; the scheduler reads the table under a mutex.
 */
#ifndef CCMA_E2_H
#define CCMA_E2_H
#include <stdint.h>

/* While the UE has UL data, each UL slot gives it at least min(floor_prb, the PRBs that empty its buffer) PRBs, new
 * transmissions and retransmissions together; what is above the floor is left to the stock scheduler (PF order, the
 * service quotas). floor_prb 0 = no floor. validity_ms 0 = until replaced; once it runs out the UE has no floor.
 * Returns 0, or -1 for a bad RNTI / a full table. */
int ccma_e2_set_ul_floor(uint16_t rnti, int floor_prb, int validity_ms);

#endif
