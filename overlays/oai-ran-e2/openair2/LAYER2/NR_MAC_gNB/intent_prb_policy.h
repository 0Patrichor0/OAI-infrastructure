#ifndef OAI_INTENT_PRB_POLICY_H
#define OAI_INTENT_PRB_POLICY_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

/* Deployment-local customized MAC SM 142 extension, preserving the 8-byte payload. */
#define INTENT_PRB_QUERY 0xd1u
#define INTENT_PRB_SET 0xd2u
#define INTENT_PRB_CLEAR 0xd3u
#define INTENT_PRB_TOKEN_MASK 0x1ffffu
#define INTENT_PRB_MAX_SLICES 16
#define INTENT_PRB_MIN_GRANT 5u

static inline uint32_t intent_prb_key(unsigned sst, unsigned sd)
{
  return (sst << 24) | (sd & 0xffffffu);
}

static inline uint32_t intent_prb_word(unsigned op, unsigned token, unsigned percent)
{
  return (op << 24) | ((token & INTENT_PRB_TOKEN_MASK) << 7) | (percent & 127u);
}

static inline bool intent_prb_is_command(uint32_t word)
{
  unsigned op = word >> 24;
  return op >= INTENT_PRB_QUERY && op <= INTENT_PRB_CLEAR;
}

typedef struct {
  bool used;
  bool enabled;
  uint8_t sst;
  uint8_t percent;
  uint32_t sd;
  uint64_t revision;
  uint32_t slot_new_prbs;
  uint32_t slot_retx_prbs;
  uint32_t slot_control_prbs;
  uint64_t new_grants;
  uint64_t new_prbs;
  uint64_t retx_prbs;
  uint64_t control_prbs;
  uint64_t limited_requests;
  uint64_t guard_rejections;
  uint64_t violations;
  uint32_t peak_new_prbs;
  uint32_t limit_at_peak;
} intent_prb_slice_t;

typedef struct {
  intent_prb_slice_t slices[INTENT_PRB_MAX_SLICES];
  unsigned width;
  unsigned num_beams;
  int frame;
  int slot;
  bool epoch_valid;
} intent_prb_state_t;

static inline intent_prb_slice_t *intent_prb_find(intent_prb_state_t *state, unsigned sst, unsigned sd, bool create)
{
  intent_prb_slice_t *free_entry = NULL;
  for (unsigned i = 0; i < INTENT_PRB_MAX_SLICES; ++i) {
    intent_prb_slice_t *entry = &state->slices[i];
    if (entry->used && entry->sst == sst && entry->sd == sd)
      return entry;
    if (!entry->used && !free_entry)
      free_entry = entry;
  }
  if (create && free_entry) {
    *free_entry = (intent_prb_slice_t){.used = true, .sst = (uint8_t)sst, .percent = 100, .sd = sd};
    return free_entry;
  }
  return NULL;
}

static inline unsigned intent_prb_limit(const intent_prb_state_t *state, const intent_prb_slice_t *entry)
{
  return state->width * (entry->enabled ? entry->percent : 100u) / 100u;
}

static inline unsigned intent_prb_remaining(const intent_prb_state_t *state, const intent_prb_slice_t *entry)
{
  unsigned limit = intent_prb_limit(state, entry);
  unsigned used = entry->slot_new_prbs + entry->slot_retx_prbs;
  return used >= limit ? 0 : limit - used;
}

static inline void intent_prb_begin_slot(intent_prb_state_t *state, int frame, int slot, unsigned width)
{
  if (state->epoch_valid && state->frame == frame && state->slot == slot)
    return;
  state->epoch_valid = true;
  state->frame = frame;
  state->slot = slot;
  state->width = width;
  for (unsigned i = 0; i < INTENT_PRB_MAX_SLICES; ++i) {
    state->slices[i].slot_new_prbs = 0;
    state->slices[i].slot_retx_prbs = 0;
    state->slices[i].slot_control_prbs = 0;
  }
}

static inline bool intent_prb_apply(intent_prb_state_t *state, uint32_t key, uint32_t word)
{
  unsigned op = word >> 24;
  unsigned token = (word >> 7) & INTENT_PRB_TOKEN_MASK;
  unsigned percent = word & 127u;
  if (!intent_prb_is_command(word) || token == 0)
    return false;
  if (op == INTENT_PRB_SET) {
    if (percent == 0 || percent > 100 || state->width * percent / 100u < INTENT_PRB_MIN_GRANT)
      return false;
  } else if (percent != 0) {
    return false;
  }
  intent_prb_slice_t *entry = intent_prb_find(state, key >> 24, key & 0xffffffu, true);
  if (!entry)
    return false;
  if (op != INTENT_PRB_QUERY) {
    uint64_t revision = entry->revision + 1;
    *entry = (intent_prb_slice_t){.used = true, .enabled = op == INTENT_PRB_SET,
                                .sst = (uint8_t)(key >> 24),
                                .percent = (uint8_t)(op == INTENT_PRB_SET ? percent : 100),
                                .sd = key & 0xffffffu, .revision = revision};
    state->epoch_valid = false;
  }
  return true;
}

/* Gate before CCE/PUCCH reservation. Failed grants must not consume budget. */
static inline bool intent_prb_can_commit(intent_prb_state_t *state, intent_prb_slice_t *entry,
                                         bool retransmission, bool has_data, unsigned prbs)
{
  if (!entry || !entry->enabled || retransmission || !has_data)
    return true;
  if (prbs <= intent_prb_remaining(state, entry))
    return true;
  entry->guard_rejections++;
  return false;
}

static inline void intent_prb_account(intent_prb_state_t *state, intent_prb_slice_t *entry,
                                      bool retransmission, bool has_data, unsigned prbs)
{
  if (!entry)
    return;
  if (retransmission) {
    entry->slot_retx_prbs += prbs;
    entry->retx_prbs += prbs;
  } else if (!has_data) {
    entry->slot_control_prbs += prbs;
    entry->control_prbs += prbs;
  } else {
    entry->slot_new_prbs += prbs;
    entry->new_prbs += prbs;
    entry->new_grants++;
    if (entry->slot_new_prbs > entry->peak_new_prbs) {
      entry->peak_new_prbs = entry->slot_new_prbs;
      entry->limit_at_peak = intent_prb_limit(state, entry);
    }
    if (entry->enabled && entry->slot_new_prbs > intent_prb_limit(state, entry))
      entry->violations++;
  }
}

#endif
