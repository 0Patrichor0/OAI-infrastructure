#include "e42_xapp_api.h"
#include "intent_prb_policy.h"
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static pthread_mutex_t samples_lock = PTHREAD_MUTEX_INITIALIZER;
static unsigned indications, observations, observed_max;

static void on_mac(sm_ag_if_rd_t const *data)
{
  if (data->type != INDICATION_MSG_AGENT_IF_ANS_V0 || data->ind.type != MAC_STATS_V0)
    return;
  pthread_mutex_lock(&samples_lock);
  indications++;
  for (size_t i = 0; i < data->ind.mac.msg.len_ue_stats; ++i) {
    const mac_ue_stats_impl_t *ue = &data->ind.mac.msg.ue_stats[i];
    observations++;
    if (ue->dl_mcs1 > observed_max)
      observed_max = ue->dl_mcs1;
  }
  pthread_mutex_unlock(&samples_lock);
}

static bool number(const char *text, unsigned *out)
{
  char *end;
  errno = 0;
  unsigned long value = strtoul(text, &end, 10);
  if (errno || *text == 0 || *end || value > UINT32_MAX)
    return false;
  *out = value;
  return true;
}

static void remove_temporary_database(void)
{
  char path[128];
  snprintf(path, sizeof(path), "/tmp/intent_prb_%d", getpid());
  unlink(path);
}

int main(int argc, char **argv)
{
  if (argc != 6) {
    fprintf(stderr, "usage: xapp_prb_control query|set|clear TOKEN SST SD PERCENT\n");
    return 2;
  }
  unsigned token, value, sst, sd;
  char *end;
  unsigned op = strcmp(argv[1], "query") == 0 ? INTENT_PRB_QUERY
                : strcmp(argv[1], "set") == 0 ? INTENT_PRB_SET
                : strcmp(argv[1], "clear") == 0 ? INTENT_PRB_CLEAR : 0;
  if (!op || !number(argv[2], &token) || token == 0 || token > INTENT_PRB_TOKEN_MASK
      || !number(argv[3], &sst) || sst > 255 || strlen(argv[4]) != 6
      || !number(argv[5], &value) || value > 100 || (op != INTENT_PRB_SET && value != 0))
    return 2;
  errno = 0;
  sd = strtoul(argv[4], &end, 16);
  if (errno || *end || sd > 0xffffff || (op == INTENT_PRB_SET && value == 0))
    return 2;
  setvbuf(stdout, NULL, _IOLBF, 0);
  fr_args_t args = init_fr_args(1, argv);
  /* Each short-lived control xApp must have its own monitoring database. */
  snprintf(args.db_name, sizeof(args.db_name), "intent_prb_%d", getpid());
  snprintf(args.db_dir, sizeof(args.db_dir), "/tmp/");
  atexit(remove_temporary_database);
  init_xapp_api(&args);
  sleep(1);
  e2_node_arr_xapp_t nodes = e2_nodes_xapp_api();
  int selected = -1;
  for (size_t i = 0; i < nodes.len; ++i) {
    if (nodes.n[i].id.type != ngran_gNB_DU)
      continue;
    for (size_t j = 0; j < nodes.n[i].len_rf; ++j) {
      if (nodes.n[i].rf[j].id != 142)
        continue;
      if (selected != -1) {
        fprintf(stderr, "Ambiguous DU selection; no control sent\n");
        free_e2_node_arr_xapp(&nodes);
        return 3;
      }
      selected = i;
    }
  }
  if (selected == -1) {
    fprintf(stderr, "No connected DU advertising MAC SM 142; no control sent\n");
    free_e2_node_arr_xapp(&nodes);
    return 3;
  }
  printf("PRB_CONTROL_TX token=%u op=%u sst=%u sd=%06x percent=%u gnb=%u\n",
         token, op, sst, sd, value, nodes.n[selected].id.nb_id.nb_id);
  mac_ctrl_req_data_t request = {.hdr.dummy = intent_prb_key(sst, sd),
                               .msg.action = intent_prb_word(op, token, value)};
  sm_ans_xapp_t answer = control_sm_xapp_api(&nodes.n[selected].id, 142, &request);
  printf("PRB_CONTROL_ACK token=%u received=%d\n", token, answer.success);
  /* Verify live MAC indications on the same E2 connection after policy application. */
  sm_ans_xapp_t sub = report_sm_xapp_api(&nodes.n[selected].id, 142, "10_ms", on_mac);
  if (sub.success) {
    usleep(1500000);
    rm_report_sm_xapp_api(sub.u.handle);
  }
  pthread_mutex_lock(&samples_lock);
  printf("PRB_TELEMETRY {\"indications\":%u,\"observations\":%u,\"observed_max_mcs\":%u}\n",
         indications, observations, observed_max);
  pthread_mutex_unlock(&samples_lock);
  free_e2_node_arr_xapp(&nodes);
  while (!try_stop_xapp_api())
    usleep(1000);
  return answer.success ? 0 : 4;
}
