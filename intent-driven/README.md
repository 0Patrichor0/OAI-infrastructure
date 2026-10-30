# 意图驱动控制面(intentctl)

把自然语言运维意图收敛成 **结构化意图 → 执行计划 → 真实配置变更 → 验证 → 可回滚** 的闭环,
落在本机 OAI 5GC(UDR/MySQL + SMF/UE)上。所有意图写入 sqlite 账本 + append-only 审计流水,可回放。

## 已支持的动作

| action | 意图语义 | 执行面 | 代价 |
|---|---|---|---|
| `add_subscriber` | 新增用户 / 补齐签约(幂等收敛) | `oai_db`:AuthenticationSubscription、SessionManagementSubscriptionData(克隆模板订户) | 不打断现有会话 |
| `set_qos` | 修改 5QI / AMBR / ARP | `dnnConfigurations` JSON | 活动 UE 断 ~10s |
| `provision_service` | 开通新业务/新切片档案(S-NSSAI + 业务 QoS) | `singleNssai` + `dnnConfigurations` | 断 ~10s |
| `set_egress` | 调整数据出口:N6 下一跳切到第二条 DN | 基础 compose 的 `IF_3_IP_REMOTE` + 新 DN 容器 + 重建 UPF | **断所有会话**,UPF+SMF 重建 |
| `boost_capacity` | 扩容:UPF VPP worker 数 | 基础 compose 的 `VPP_CORE_WORKER`(核列表)+ 重建 UPF | **断所有会话** |
| `reduce_latency` | 降时延:切低时延 5QI 档并实测对比 | `5gQosProfile.5qi` | 断 ~10s |
| `sla_guard` | 保障 SLA:按 KPI 判违约 → 补偿 → 复测 | KPM(`DRB.UEThpDl`/`DRB.RlcSduDelayDl`)+ ping + QoS 补偿 | 违约时断 ~10s;达标不动 |
| `set_ran_dl_mcs` | DU 小区下行 MCS 上限 | xApp → FlexRIC → E2 → DU MAC | 热更新；可回读、回滚，DU 重启复位 |
| `set_ran_slice_prb` | 同切片 UE 共享的下行新传 PRB 比例上限 | xApp → FlexRIC → E2 → DU MAC 调度器 | 热更新；可回读、取消、回滚，DU 重启复位 |

> RAN 能力已于 2026-10-27 通过实际 E2 注册和源码交叉核实：CU-CP 宣告 RC Style 1/3；
> Style 1 的 QoS 映射仅打印，未真正应用；Style 3 有切换执行代码，但当前单小区未做端到端验证。
> DU 未宣告标准 RC 控制。2026-10-29 已通过定制 MAC SM 142 实现 MCS 上限和切片下行新传 PRB 上限；后者不是最低带宽预留或完整切片隔离。
> 详细证据与接入边界见 [RAN_CAPABILITIES.md](docs/RAN_CAPABILITIES.md)。


## 用法

```bash
cd /home/ubuntu/intent-driven

# 只看解析结果(不碰现网),用于审计"这条自然语言被理解成了什么"
./intentctl.py parse "给新用户 IMSI 208950000000099 开通,5QI 9,上下行各 50Mbps"

# 先看计划,不动手
./intentctl.py apply --nl "把 208950000000031 的 5QI 改成 9,上下行各 200Mbps" --dry-run

# 执行(会重建会话)
./intentctl.py apply --nl "把 208950000000031 的 5QI 改成 9,上下行各 200Mbps" --yes

# 账本与回放
./intentctl.py list
./intentctl.py show   <id>
./intentctl.py verify <id>      # 重新验证
./intentctl.py rollback <id>    # 按快照回滚(活动 UE 会顺带重建会话)
./intentctl.py replay <id>      # 用账本里的结构化意图重新执行(不再走 NL 解析)
```

`--no-llm` 强制走确定性规则解析;不加时若有 LLM 配置则优先 LLM,失败自动退回规则。

## 自然语言解析

- 规则解析(默认可用):关键词 + 正则,覆盖 IMSI / 5QI / AMBR / SST / SD / 静态地址 / ARP 优先级。
  解析到关键词但取不到数值时,会打 `⚠ 警告` 并把置信度降下来,不会静默丢字段。
- LLM 解析(需配置,OpenAI 兼容):

```bash
export INTENT_LLM_BASE=https://api.deepseek.com
export INTENT_LLM_KEY=<your key>
export INTENT_LLM_MODEL=deepseek-chat
```

  两类解析都会在意图的 `parser` 字段里标注 `type/model/confidence`,审计流水里也有。

## 意图结构与例子

```json
{
  "action": "set_qos",
  "target": {"imsi": "208950000000031"},
  "params": {"fiveQI": 9, "ambr_ul": "200Mbps", "ambr_dl": "200Mbps"},
  "constraints": {"allow_session_rebuild": true}
}
```

`add_subscriber` 的 `params` 额外支持:`template_imsi`(克隆哪个订户的密钥与结构,默认
`208950000000031`)、`sst`、`sd`、`static_ip`、`arp_priority`、`mode`(`auto|create|ensure`)。
`mode=auto` 时:库里已有部分数据 → 只补缺失的表并收敛字段(意图幂等);`create` 遇到已存在会报错。

## 状态与审计

| 项 | 位置 |
|---|---|
| 账本(sqlite) | `ledger.db` / 表 `intents` |
| 审计流水(JSONL) | `audit/audit.jsonl`(parsed / snapshot / executed / verified / rolled_back / error) |
| 回滚快照 | 账本 `snapshot_json` 列(mysqldump 的行级 SQL) |
| 运行日志 | 由 stdout 决定,`systemd` 场景建议重定向到 `~/oai-ran-logs/` |

## 实测结果(2026-10-26)

- 新增用户 `208950000000099`:库里原有鉴权行、无签约 → `mode=auto` 只补签约,
  5QI 9 / 50Mbps / SST 1 全部回读一致;`AuthenticationSubscription` 复用原有密钥。
- 修改 QoS `208950000000031`:5QI 6→9、AMBR 100→200Mbps,9s 内完成会话重建
  (`PDU Session Establishment Accept`,IP 12.1.1.3),ext-dn 192.168.73.135 可达。
- 回滚链:先回滚新意图(回到 5QI 6 / 200Mbps),再回滚上一个(回到基线 5QI 6 / 100Mbps),
  每次都重建会话(~9s)并验证用户面可达。

## 已知边界

- **QoS / 切片类改动要重建 PDU 会话才生效**(OAI 在会话建立时读签约数据)。
- **UPF 重建会让 SMF 失去 PFCP 关联**(`PDU session does not have a UPF association`),
  所以 `set_egress` / `boost_capacity` 在重建 UPF 后会自动 `docker restart oai-smf`,
  代价是所有会话重建;`SmfSelectionSubscriptionData` / AMF 侧数据不受影响。
- **`VPP_CORE_WORKER` 是核列表不是数量**:镜像模板是 `corelist-workers $VPP_CORE_WORKER`,
  写 `2` 只得到 1 个 worker(core 2),要 2 个 worker 得写 `1,2`。动作里按 worker 数生成核列表。
- **不要用 compose override 改 UPF 环境变量**:list 形式会被追加而不是覆盖(base 值仍生效);
  而且 `start-all.sh`/`ue-repair.sh` 重建时只用基础 compose,override 会丢。
  正确做法是改基础 compose 的 env 值(动作已这么做,快照里存旧值可回滚)。
- **UE 在 UPF/SMF 重建后第一次附着常失败**,需要重试(动作里最多 3 轮 × 90s,每轮重新 restart)。
- **出口切换不能用"ping 旧 DN 不通"当判据**:第二条 DN 是容器路由器,仍能把包转给旧 DN。
  正确判据是"新 DN 网卡 rx 增长、旧 DN rx 不变"。
- `static_ip` 与在用 UE 冲突会导致会话建立失败,意图里会给出提示但不会自动避让。
- 模板订户只有 `AuthenticationSubscription` + `SessionManagementSubscriptionData` 两类数据;
  本环境 `SmfSelectionSubscriptionData` 全表为空、`AccessAndMobilitySubscriptionData` 只有 5 行,
  AMF 侧目前不依赖它们,故不克隆。
- 新增动作:在 `intentlib/actions.py`(或 `actions_more.py`)实现
  `validate/plan/snapshot/execute/verify/rollback`,注册进 `ACTIONS` 即可。

## Web 前端（意图驱动控制面）

```bash
sudo systemctl status intent-ui        # 常驻服务，监听 0.0.0.0:8099
# 浏览器打开 http://<本机IP>:8099
# 或从 Windows 走 SSH 隧道: ssh -L 8099:127.0.0.1:8099 ubuntu@<host>
```

页面能力：

- **顶部状态条**：UE 会话/IP/ping、SMF 的 N4 心跳、RAN 8 个单元、容器数、UPF worker、NWDAF qos 长度、AI Fabric 部署
- **下发意图**：自然语言输入 →「只解析」看结构化结果 + 执行计划 + 校验结果（不执行），或「执行意图」直接跑
- **执行日志**：动作跑几分钟，日志通过 SSE 实时刷（`/api/jobs/<id>/stream`）
- **意图账本**：点任意一行看详情——原文、参数、约束、计划、验证证据逐条、错误、快照，带「重新验证 / 回放 / 回滚」
- **持久层**：签约 IMSI（SQL 重放）+ 配置期望值（compose env / 辅助容器），一键「立即同步」
- **网络切片与 UE**：切片清册（RAN 广告 / UPF 注册 / UE 请求 / 当前会话 四面对齐，标出是否可用）、
  每个订户的签约切片与是否在跑、新增 UE、新建切片并生效、按 IMSI 激活 UE
- **NWDAF 采集数据**：直接只读 AnLF 的 Mongo（`testing.smf` / `testing.amf`）——SMF/AMF 文档数、QoS 监控样本数、每 SUPI 的会话数与末条用量（↓/↑ KB）、数据新鲜度、SBI 近 2 分钟写入条数；8 秒自动刷新
  - 对账触发条件有三条：SQL 文件变化 / mysql 容器比上次应用更新（重建过）/ 持久层里的 IMSI 在库里缺失；
    只看文件 hash 会漏掉 L3 重建（`applied.sha` 在磁盘上活着，DB 已被种子重置）

后端 `ui/server.py` 只用标准库（`http.server`），**所有写操作都走 `intentctl.py` 子进程**，
不复制业务逻辑，账本/审计/回滚仍然只有一套实现。

主要接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system` | 现状快照（UE/N4/单元/容器/worker/NWDAF） |
| GET | `/api/nwdaf` | NWDAF 采集数据（AnLF Mongo 聚合：smf/amf 文档、QoS 样本、新鲜度、SBI 写入速率） |
| GET | `/api/intents?limit=` | 账本列表 |
| GET | `/api/intents/<id>` | 单条详情（含 verify/snapshot/plan） |
| POST | `/api/parse` | `{text}` → 结构化意图 + plan + 校验（不落库） |
| POST | `/api/apply` | `{text}` → 起 job，返回 `{id}` |
| POST | `/api/intents/<id>/{verify,rollback,replay}` | 起对应 job |
| GET | `/api/jobs/<id>/stream` | SSE 日志流 |
| GET | `/api/persist` | 持久层现状 |
| POST | `/api/persist/apply` | 同步 DB + 配置 |


## 切片与 UE 的三个硬约束(实测)

1. **一个切片要可用,必须四处对齐**:RAN `snssaiList`、UPF 的 `SNSSAI_SST/SD`、订户 `singleNssai`、
   UE `ue.conf` 的 `nssai_sst/sd`。少一处就建不起会话。
2. **核心(NRF 的 AMF/SMF 配置)也必须认识这个 S-NSSAI**。配置在宿主机挂载文件
   `nwdaf-rel17/oai-cn5g-fed/docker-compose/conf/basic_vpp_nrf_config.yaml`,
   核心不认识时 SMF 报 `UPF could not be selected`、UE 侧会话被拒。
   `activate_slice` 会按需把新切片写进该文件(anchor + AMF nssai 列表 + SMF 两张表)并重启 AMF/SMF。
3. **本环境只有一个 UPF、一个 UE 实例**:
   - UPF 的 S-NSSAI 是单值 → 同一时刻只能有一个切片可用(多切片并存需要第二个 UPF);
   - nr-uesoftmodem 的 tun 名固定 `oaitun_ue1` 且没有 `--tun` 参数 → 只能跑一个 UE,
     "新增 UE" 的语义是**新增订户 → 用 switch_ue 把活动 UE 切过去**。

新增的两个动作:`activate_slice`(新建/激活切片)、`switch_ue`(把活动 UE 切到指定订户),
都可回滚(切片会恢复 RAN/UE/UPF/核心配置四处快照)。

## 多 UE 节点（add_ue）

三种动作的语义别混：

| 动作 | 做什么 | 会多一个 UE 进程吗 |
|---|---|---|
| `add_subscriber` | 只在 UDR/MySQL 里加签约 | 不会 |
| `switch_ue` | 把**唯一**那个 UE 切到另一个订户 | 不会 |
| `add_ue` | 起一个**新的 UE 节点**（独立 netns + 独立进程 + 独立 systemd 单元） | 会 |

### 为什么必须用 netns

- 一个 `nr-uesoftmodem` 进程 = 一个 UE；
- tun 名由 `tun_generate_ifname("oaitun_ue", instance_id)` 合成，单进程实例号恒为 1
  → 再起一个进程必然撞 `oaitun_ue1`；
- 放进独立 netns 后各自的 `oaitun_ue1` 互不冲突。DU 的 rfsim 监听 `*:4043`，
  netns 里通过 veth 连宿主即可。

### 约定（代码里写死，见 `intentlib/ue_nodes.py`）

```
第 idx 个 UE:  netns ue{idx}, 宿主 10.250.{idx}.1/24 ↔ netns 10.250.{idx}.2/24
配置           /home/ubuntu/oai-ran-conf/ue{idx}.conf
单元           oran-ue{idx}.service（ExecStart = ip netns exec ue{idx} nr-uesoftmodem ...）
日志           /home/ubuntu/oai-ran-logs/ue{idx}.log
```

`ue-netns-setup.sh {idx}` 是幂等的 netns/veth 创建脚本，挂在单元的 `ExecStartPre` 上，
所以**宿主重启后 netns 会自动重建**。

### 用法

```bash
cat > /tmp/ue.json <<'EOF'
{"action":"add_ue","target":{"imsi":"208950000000095"},
 "params":{"sst":3,"sd":"000003"},"constraints":{}}
EOF
./intentctl.py apply --file /tmp/ue.json --yes
```

订户不存在时会自动开户（复用 `add_subscriber`）；切片必须在 UPF 注册过，否则直接拒绝
并提示先 `activate_slice`。

UI 上“新增 UE 节点”按钮走的就是这个动作（`POST /api/ues`），右侧「UE 节点」表按
`/api/ue-nodes` 展示每个节点的 netns / 状态 / IMSI / 会话 IP。

### 持久化

`state/intent_config.json` 的 `ue_nodes` 记 `{index, imsi, sst, sd}`；
`tools/apply_state.py`（每 2 分钟 + `start-all.sh` step2）调用 `ue_nodes.ensure_node()`：
netns 没了重建、conf 没了按 DB 里的 K/OPc 重生、单元没了重写、没跑就拉起来。
实测把 unit + conf + netns 全删掉，`apply_state.py` 一次就把节点恢复（新会话 IP，ping 通）。

回滚会整包清理（停单元、删 conf、删 netns/veth、删单元文件），**订户签约保留**。


## E2 DU MCS control added on 2026-10-29

A new `set_ran_dl_mcs` intent drives a control xApp through E42/FlexRIC and E2 customized MAC SM 142. The DU applies a cell-wide DL MCS ceiling under its scheduler mutex. Runtime readback and MAC indications were observed at 28 -> 20 -> 18 -> 20 -> 28 while RIC/CU/DU/UE PIDs stayed unchanged. All three UEs remained reachable. This does not implement standard E2SM-RC, CU writes, PRB quotas, or slice resource isolation. The policy resets on DU restart.

See `E2_MCS_CONTROL_20261029.md` for scope, commands, source paths and backups.


## 切片 PRB 控制（2026-10-29）

页面可选择切片并在线下发、回读、取消上限，或输入：`将切片 SST 3 SD 000003 的下行 PRB 上限设为 30%`。同切片全部 UE 共用预算；HARQ 重传及纯控制授权单独统计。策略不跨 DU 重启保留；`start-all.sh` 无需修改，重启后请重新下发。

实际两 UE 测试、接口、边界及备份见 [切片 PRB 控制报告](docs/PRB_CONTROL_20261029.md)。
