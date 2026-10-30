# RAN capabilities verified on 2026-10-27, rechecked 2026-10-28

## Evidence and scope

Read-only E42 capability probe queried the running FlexRIC instance, not just
repository documentation. Raw output and probe source are retained under
`/home/ubuntu/agent-repair-20261027/tests/`.

Observed nodes: OAI CU-CP and DU, both gNB ID 3584. RF simulation is enabled.
The CU-UP process is running, but is not one of the two E2 nodes in this capture.
No separate physical RU was verified.

| Capability | Advertised | Implementation / verification | Agent access |
|---|---|---|---|
| KPM and MAC/RLC telemetry | Yes | Running monitoring xApps receive indications | Read-only; validate timestamps and metric semantics |
| CU-CP RC Style 1, Action 2: QoS flow mapping | Yes | `write_ctrl_radio_bearer()` only decodes and prints; application is explicitly unimplemented | Do not expose as an effective write action |
| CU-CP RC Style 3, Action 1: handover | Yes | `write_ctrl_conn_mode_mobility()` calls F1/N2 handover paths; binary contains RC handler | Not end-to-end verified; requires a distinct configured target cell |
| DU RC control | No | Live `defn.rc.ctrl` is null | Unavailable through RC in this deployment |
| Slice DL new-transmission PRB ceiling | Custom MAC SM 142 | Verified on 2026-10-29: two UEs share 31/106 PRBs at 30%, 53/106 at 50%; zero recorded violations | `set_ran_slice_prb`, runtime readback, clear and rollback |
| Minimum PRB reservations / complete RAN slice isolation | No | Not implemented; HARQ/control exemptions and other layers remain | Do not promise guaranteed bandwidth or full isolation |
| Existing intentctl core-network actions | Separate from E2 | Existing code has validate/plan/execute/verify/rollback; no write action was exercised in this audit | Gate actions by their individual validation and disruption scope |

Current topology has one DU/cell (NR cell identity 12345678). A successful
handover cannot be demonstrated without another reachable configured cell.
No handover, PRB reallocation or RAN slicing command was sent during this audit.
This report resolves the capability uncertainty; it does not implement missing
RAN controls or certify a successful handover.

## Repairs and validation

1. The NF watchdog ran as ubuntu but tried to create a lock in `/run` directly.
   Its unit now uses a systemd-managed `RuntimeDirectory=oai-nf-watchdog` and
   `/run/oai-nf-watchdog/watchdog.lock`.
2. With `pipefail`, `docker logs | grep -q` returned 141 even when the match
   existed. Notification verification now drains the pipe with grep redirected
   to `/dev/null`. The real failing pipeline returned 141 before and 0 after.
3. RIC duplicated high-rate telemetry into an unbounded `log.txt`, which was
   omitted from the existing log guard and hit `LimitFSIZE=200M`. A deployment
   switch `FLEXRIC_STATS_LOG=0` disables only that duplicate listener output.
   xApp telemetry collection remains enabled. A test using the real logger and
   a 64 KiB file limit reproduced SIGXFSZ before and passed after the patch.
4. Late indications lacking an xApp mapping now emit a counted message once
   per 10,000 arrivals rather than one line per packet. This limits log growth;
   it is not a claim that all stale-subscription lifecycle bugs are resolved.
5. The RIC was rebuilt and restarted. CU-CP, DU and both running simulated UEs
   were restarted once to renew E2 registrations. Automatic xApp and UE timers
   were then restored. No core configuration or subscriber values were changed.

6. On 2026-10-28 two SEGV instruction addresses mapped to missing E2-node
   lookup and map extraction. Calling the real mapping functions with an
   unknown node/association reproduced SIGSEGV. Missing-node lookups now return
   a zeroed address, the endpoint drops that queued message, and duplicate or
   pre-Setup shutdown notifications return without removing an absent node.
   All three regression cases (missing lookup, missing removal, registered
   lookup followed by duplicate removal) failed before and passed after.
   Source and before/after JSON results are in the tests backup directory.
   Stability remains bounded by the recorded observation window; this is not
   a claim that all possible FlexRIC bugs are eliminated.

Old SUPI documents remain in MongoDB intentionally. Their old timestamps do
not establish a failure for currently inactive subscribers. Compare freshness
against the UE identities actually running, not just the first stored document.

## Rollback material

Original source files, unit files and the original RIC executable are retained
under `/home/ubuntu/agent-repair-20261027/`. The pre-existing modification to
`src/ric/plugin_ric.c` was preserved. No git commit was created.
New RIC service override: `/etc/systemd/system/flexric-ric.service.d/20-stats-log.conf`.
Do not restore old files over later edits without reviewing their differences.


## E2 DU MCS control added on 2026-10-29

A new `set_ran_dl_mcs` intent drives a control xApp through E42/FlexRIC and E2 customized MAC SM 142. The DU applies a cell-wide DL MCS ceiling under its scheduler mutex. Runtime readback and MAC indications were observed at 28 -> 20 -> 18 -> 20 -> 28 while RIC/CU/DU/UE PIDs stayed unchanged. All three UEs remained reachable. This does not implement standard E2SM-RC, CU writes, PRB quotas, or slice resource isolation. The policy resets on DU restart.

See `E2_MCS_CONTROL_20261029.md` for scope, commands, source paths and backups.


## Slice DL new-transmission PRB ceiling added on 2026-10-29

The `set_ran_slice_prb` intent now drives a control xApp through FlexRIC and E2 customized MAC SM 142 into the DU MAC scheduler. All UEs identified by the same DRB S-NSSAI share each DL-slot ceiling. The default PF allocation is clamped and final grant commitment is checked. HARQ retransmissions and pure control grants are treated separately. This is a usage ceiling, not a minimum reservation or throughput guarantee.

Two live RFsim UEs in 3/000003 received downlink load while the slice aggregate new-data peak remained at 31 PRBs for 30% of a 106-PRB BWP, then at 53 for 50%, with zero recorded violations. Hot updates, clear and nested rollback retained the RIC/CU/DU/UE PIDs. Temporary UE4 was removed and the original three UEs remained reachable. Current scope is cell 0, one DU, one beam, one slice per UE; policy resets on DU restart. Earlier sections retain the dated baseline observations.

See [PRB_CONTROL_20261029.md](PRB_CONTROL_20261029.md) for evidence, limitations, temporary read failure, APIs, source and backups. `PRB_FINAL_HEALTH_20261029.json` records final checks.
