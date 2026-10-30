#include "intent_mcs_policy.h"
#include <assert.h>
#include <stdio.h>

int main(void)
{
  uint8_t cap = 28;
  assert(intent_mcs_apply(INTENT_MCS_MAGIC, intent_mcs_word(INTENT_MCS_QUERY, 1, 0), 4, &cap));
  assert(cap == 28);
  assert(intent_mcs_apply(INTENT_MCS_MAGIC, intent_mcs_word(INTENT_MCS_SET, 2, 20), 4, &cap));
  assert(cap == 20);
  assert(!intent_mcs_apply(1, 42, 4, &cap));
  assert(!intent_mcs_apply(INTENT_MCS_MAGIC, intent_mcs_word(INTENT_MCS_SET, 3, 29), 4, &cap));
  assert(!intent_mcs_apply(INTENT_MCS_MAGIC, intent_mcs_word(INTENT_MCS_SET, 4, 3), 4, &cap));
  assert(!intent_mcs_apply(INTENT_MCS_MAGIC, intent_mcs_word(3, 5, 20), 4, &cap));
  assert(!intent_mcs_apply(INTENT_MCS_MAGIC, intent_mcs_word(INTENT_MCS_SET, 0, 20), 4, &cap));
  assert(!intent_mcs_apply(INTENT_MCS_MAGIC, intent_mcs_word(INTENT_MCS_QUERY, 6, 20), 4, &cap));
  assert(cap == 20);
  assert(intent_mcs_apply(INTENT_MCS_MAGIC, intent_mcs_word(INTENT_MCS_SET, 7, 28), 4, &cap));
  assert(cap == 28);
  puts("PASS: production MCS policy query, apply, rejection without mutation, and restore");
}
