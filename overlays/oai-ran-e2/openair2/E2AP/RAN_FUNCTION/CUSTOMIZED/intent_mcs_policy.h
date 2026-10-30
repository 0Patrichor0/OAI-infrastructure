#ifndef OAI_INTENT_MCS_POLICY_H
#define OAI_INTENT_MCS_POLICY_H

#include <stdbool.h>
#include <stdint.h>

/* Deployment-local extension of FlexRIC's customized MAC SM (RAN function 142).
 * The standard MAC payload layout is unchanged; this is not E2SM-RC. */
#define INTENT_MCS_MAGIC UINT32_C(0x4d435331)
#define INTENT_MCS_QUERY 1u
#define INTENT_MCS_SET 2u
#define INTENT_MCS_TOKEN_MASK UINT32_C(0xffffff)

static inline uint32_t intent_mcs_word(unsigned op, uint32_t token, unsigned value)
{
  return (op << 30) | ((token & INTENT_MCS_TOKEN_MASK) << 6) | (value & 63u);
}

/* Called with the scheduler mutex held. A rejected request cannot change policy. */
static inline bool intent_mcs_apply(uint32_t magic, uint32_t word, uint8_t minimum, uint8_t *maximum)
{
  unsigned op = word >> 30;
  unsigned value = word & 63u;
  if (magic != INTENT_MCS_MAGIC || ((word >> 6) & INTENT_MCS_TOKEN_MASK) == 0)
    return false;
  if (op == INTENT_MCS_QUERY)
    return value == 0;
  if (op != INTENT_MCS_SET || value > 28 || value < minimum)
    return false;
  *maximum = value;
  return true;
}

#endif
