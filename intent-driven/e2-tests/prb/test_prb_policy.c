#include "intent_prb_policy.h"
#include <assert.h>
#include <stdio.h>

int main(void)
{
  intent_prb_state_t s = {.width = 106};
  uint32_t key = intent_prb_key(3, 3);
  assert(intent_prb_apply(&s, key, intent_prb_word(INTENT_PRB_SET, 1, 30)));
  intent_prb_slice_t *p = intent_prb_find(&s, 3, 3, false);
  assert(p && p->enabled && p->revision == 1);
  intent_prb_begin_slot(&s, 5, 1, 106);
  assert(intent_prb_limit(&s, p) == 31);
  assert(intent_prb_can_commit(&s, p, false, true, 20));
  intent_prb_account(&s, p, false, true, 20);
  /* A second UE in the same slice shares the remaining eleven PRBs. */
  assert(!intent_prb_can_commit(&s, p, false, true, 20));
  assert(intent_prb_remaining(&s, p) == 11);
  assert(intent_prb_can_commit(&s, p, false, true, 11));
  intent_prb_account(&s, p, false, true, 11);
  assert(p->peak_new_prbs == 31 && p->violations == 0);
  intent_prb_begin_slot(&s, 5, 1, 106);
  assert(intent_prb_remaining(&s, p) == 0); /* another beam cannot reset the slot */
  intent_prb_begin_slot(&s, 5, 2, 106);
  intent_prb_account(&s, p, true, true, 20);
  assert(intent_prb_remaining(&s, p) == 11);
  assert(intent_prb_can_commit(&s, p, true, true, 40)); /* HARQ reliability */
  intent_prb_account(&s, p, true, true, 40);
  assert(intent_prb_remaining(&s, p) == 0);
  assert(intent_prb_can_commit(&s, p, false, false, 5)); /* TA/control messages */
  intent_prb_account(&s, p, false, false, 5);
  assert(p->control_prbs == 5 && p->violations == 0);
  assert(!intent_prb_apply(&s, key, intent_prb_word(INTENT_PRB_SET, 2, 1)));
  assert(!intent_prb_apply(&s, key, intent_prb_word(INTENT_PRB_SET, 0, 50)));
  assert(!intent_prb_apply(&s, key, intent_prb_word(INTENT_PRB_SET, 2, 101)));
  assert(p->percent == 30 && p->revision == 1);
  assert(intent_prb_apply(&s, key, intent_prb_word(INTENT_PRB_QUERY, 3, 0)));
  assert(p->revision == 1 && p->new_prbs == 31);
  assert(intent_prb_apply(&s, intent_prb_key(1, 124), intent_prb_word(INTENT_PRB_SET, 4, 50)));
  assert(intent_prb_find(&s, 1, 124, false)->percent == 50 && p->percent == 30);
  assert(intent_prb_apply(&s, key, intent_prb_word(INTENT_PRB_CLEAR, 5, 0)));
  assert(!p->enabled && p->revision == 2);
  assert(intent_prb_can_commit(&s, p, false, true, 106));
  /* Exercise rounding and all legal percentages using the production helper. */
  for (unsigned percent = 5; percent <= 100; ++percent) {
    assert(intent_prb_apply(&s, key, intent_prb_word(INTENT_PRB_SET, percent, percent)));
    intent_prb_begin_slot(&s, 6, percent, 106);
    unsigned limit = 106 * percent / 100;
    assert(intent_prb_can_commit(&s, p, false, true, limit));
    intent_prb_account(&s, p, false, true, limit);
    assert(!intent_prb_can_commit(&s, p, false, true, 1));
    assert(p->violations == 0);
  }
  puts("PASS: shared slice budget, slot reset, HARQ/control exceptions, invalid commands, independence and rounding");
}
