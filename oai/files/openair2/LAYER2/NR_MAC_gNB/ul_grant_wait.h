#ifndef OAI_UL_GRANT_WAIT_H
#define OAI_UL_GRANT_WAIT_H
#include <stdbool.h>
#include <stdint.h>

/* Radio-time observer: sampled once per scheduler slot under sched_lock.
 * Not BSR reception age, SR age, HOL delay, or successful transmission time.
 * A new grant ends the current interval; residual backlog starts another.
 */
typedef struct {
  bool initialized, pending;
  uint32_t previous_slot, slots_per_frame;
  uint64_t current_us, sum_us, count;
} ul_grant_wait_t;

static inline void ul_grant_wait_observe(ul_grant_wait_t *w, unsigned slot,
                                         unsigned nslots, bool backlog)
{
  if (!nslots) return;
  const unsigned period = 1024 * nslots;
  slot %= period;
  if (w->initialized && w->slots_per_frame == nslots && w->pending && backlog) {
    const unsigned delta = (slot + period - w->previous_slot) % period;
    /* Discontinuities are not evidence of congestion. Normal SFN wrap is 1. */
    if (delta <= nslots)
      w->current_us += (uint64_t)delta * 10000 / nslots;
    else
      w->current_us = 0;
  } else {
    w->current_us = 0;
  }
  w->initialized = true;
  w->previous_slot = slot;
  w->slots_per_frame = nslots;
  w->pending = backlog;
}

static inline void ul_grant_wait_complete(ul_grant_wait_t *w, bool residual)
{
  if (!w->initialized) return;
  if (w->pending) {
    w->sum_us += w->current_us;
    w->count++;
  }
  w->current_us = 0;
  w->pending = residual;
}
#endif

