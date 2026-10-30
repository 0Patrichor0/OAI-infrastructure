# 切片下行新传 PRB 上限控制

已于 2026-10-29 部署至 `ubuntu@YOUR_SERVER_IP`，并在实际运行的 RFsim CU/DU 和 UE 上验证。

## 使用方式

刷新 [意图控制页面](http://YOUR_SERVER_IP:8099/)，在“切片下行新传 PRB · E2”选择已就绪切片，填写整数百分比并下发。可以读取当前策略和统计、取消限额，或在意图账本中逆序回滚。选中切片后，每 20 秒自动更新统计；切换选择后，旧请求的返回结果不会覆盖新切片。

自然语言：

```text
将切片 SST 3 SD 000003 的下行 PRB 上限设为 30%
取消切片 SST 3 SD 000003 的 PRB 上限
```

CLI：

```bash
cd /home/ubuntu/intent-driven
python3 intentctl.py apply --nl '将切片 SST 3 SD 000003 的下行 PRB 上限设为 30%' --no-llm --yes
python3 intentctl.py rollback <意图ID>
```

接口：

```text
GET /api/ran/prb?sst=3&sd=000003
POST /api/ran/prb
{"sst":3,"sd":"000003","enabled":true,"max_prb_percent":30}

POST /api/ran/prb
{"sst":3,"sd":"000003","enabled":false}
```

## 实际执行链

```text
自然语言 / 页面表单
  → set_ran_slice_prb：校验、快照、账本
  → xapp_prb_control：E42 控制 xApp
  → FlexRIC near-RT RIC
  → E2 RIC CONTROL，部署定制的 MAC SM 142
  → DU E2 agent 在调度锁内更新切片策略
  → DU MAC PF 分配阶段裁剪预算，最终提交阶段再次检查
  → E2 查询回读 + DU 执行回执 + MAC 指示 + 目标 UE 用户面验证
```

UE 的切片身份来自 CU 经 F1 下发到 DU 的 DRB S-NSSAI，不从 IMSI 推测。相同切片的全部 UE 共用每个下行时隙的预算，而不是每个 UE 各有一份。

E2 ACK 本身不会被当作执行成功。后端同时检查 root 所有、不可被组或其他用户写入的 DU 回执，核对请求 token、操作、SST/SD、时间及实际 DU PID，并再次通过 E2 查询。配置按切片分别维护 revision 和操作归属；后续操作未回滚时，拒绝用旧快照覆盖它。DU 重启后拒绝旧 PID 的快照。

## 真实 UE 测试结果

测试切片为 `3/000003`，UE3（IMSI 尾号 095）和临时 UE4（尾号 098）同时接入，DU 回读 `matched_users=2`。其他两个 UE 保持原切片。

| 操作 | 回读 / 实测结果 |
|---|---|
| 自然语言下发 30% | 生效；106 PRB BWP 中，共享新传上限为 31 PRB |
| 两个 UE 同时接收 8 秒下行 UDP 负载 | 两个 UE 分别收到约 20.1、20.7 MB；共享新传峰值 31 PRB |
| 限额实际触发 | `new_grants=21383`，`new_prbs=658271`，`limited_requests=42762`，`violations=0` |
| 表单 API 下发 50% | 回读上限 53 PRB，实测峰值 53，越限 0 |
| 先回滚旧的 30% 操作 | 按设计拒绝；50% 保持生效 |
| 回滚 50% | 恢复 30% |
| 取消限额及回滚该取消操作 | 未限额 → 恢复 30% |
| 回滚最初的 30% 操作 | 恢复未限额 |
| MCS、PRB 并发查询 | 均收到各自匹配的回执和 MAC 遥测 |
| 热更新期间 | RIC/CU/DU/原有 UE PID 均不变；DU PID 为 2215028 |

最后完成的一整轮报告：`/home/ubuntu/e2-prb-live-20261029-154933/results.json`。自然语言、50% 和取消操作对应意图 `261029154959-9477a3`、`261029155032-dfd252`、`261029155119-255aa9`，均已回滚。

独立真实调度器基准 `nr_dlbench` 中，三个同切片模拟 UE 共用 30% 上限，200 次新传授权总计 6200 PRB，峰值 31，越限 0。加入模拟 BLER 0.1 后仍保持峰值 31、越限 0，另记录 634 个重传 PRB。基准使用生产调度代码；C 测试直接包含生产策略头文件。

其他验证：10 项 PRB 后端测试、9 项原 MCS 测试、24 项既有意图回归、1 个 CTest 策略测试以及 7 组前端检查通过。实际 API 拒绝非整数/范围外上限；未接入目标切片 DRB 和低于最小 5 PRB 的策略执行失败且不改变运行策略。最终又完成三组 MCS/PRB 并发回读。

## 当前状态与边界

- 测试结束已恢复未限额，MCS 保持 28；临时 UE4 的服务、配置、netns 已移除。三个原有 UE 的 ping 正常，监控 xApp 和六个守护定时器均运行，RIC/DU 的服务自动重启计数为 0。
- 原有 RAN/UE 配置和意图配置文件逐字节一致。临时 UE 鉴权会推进序列号，`capture()` 因而更新其持久化鉴权快照；单独比较确认，其他持久化内容和该 UE 的订阅保持一致。
- 这是 **BWP PRB 数量的下行新传使用上限**，不是最低预留、吞吐量保证或完整切片隔离。HARQ 重传允许优先执行，并扣减当时可供新传使用的预算；纯控制授权豁免。两者单独统计，因此不能把所有 PDSCH 总占用都解释为受同一个硬上限限制。
- 当前验证范围为单 DU、cell 0、单波束、每 UE 一个切片 DRB 的 RFsim 环境；不支持上行配额、多波束或跨 DU 全局预算。最多跟踪 16 个 S-NSSAI。未验证物理 RU。
- 策略仅存在于 DU 运行态。DU 重启、服务器重启或运行 `start-all.sh` 重启 DU 后恢复未限额，需要重新下发；本次无需修改 `start-all.sh`。
- 这是本部署定制 MAC 服务模型协议扩展，不是标准 E2SM-RC 控制能力。
- 初次加载版本时有一次 RAN 服务重启；随后的策略下发、回读、取消和回滚均不重启服务。
- 第一轮检查曾出现一次 MCS GET 503，未保存当次错误正文，原因尚未确定。其后两轮实际控制测试和最终三组并发回读未复现。不能据此承诺所有 FlexRIC 生命周期问题均已消除。首次完整测试在清理末尾因对鉴权快照作字节比较而触发测试断言；调整为允许临时 UE 的鉴权刷新后，整轮复测完成。原始记录保留供审查。

## 代码与备份

后端：`/home/ubuntu/intent-driven/intentlib/actions_prb.py`、`prb_control.py`；页面及 API：`ui/app.js`、`ui/index.html`、`ui/server.py`。

控制 xApp 的可执行文件和源代码在 `/home/ubuntu/intent-driven/ran/xapp_prb_control{,.c}`；短期 MAC 遥测 SQLite 数据库在 xApp 正常退出时清理。

OAI 修改：`openair2/LAYER2/NR_MAC_gNB/intent_prb_policy.h`、`nr_mac_gNB.h`、`gNB_scheduler_dlsch.c`、`gNB_scheduler_dlsch_default_policies.c`，以及 `openair2/E2AP/RAN_FUNCTION/CUSTOMIZED/ran_func_mac.c`。同时修正 MAC 指示中 UE 数量和 UE 数组获取之间的加锁位置，以便 UE 接入期间读取同一份列表。

RAN 原源码/二进制备份：`/home/ubuntu/agent-repair-e2-prb-20261029-144803`。前后端部署备份路径见 `deployment.json`。原有 FlexRIC 稳定性补丁、MCS 控制和 NAS 修改均保留；此次 PRB 扩展不改变 MAC SM 的八字节线格式，不需要再次替换 RIC 服务模型库。

生产构建：

```bash
cmake --build /home/ubuntu/oai-ran-e2/cmake_targets/ran_build/build --target nr-softmodem nr-cuup nr_dlbench -j6
python3 /home/ubuntu/intent-e2-prb-stage/build_xapp_remote.py
```

`nr_dlbench -u3 -R106 -n200 -Q30 -L0` 验证共享预算，加入 `-B0.1` 验证重传场景。构建保留上游 `conf_file.c.o` 可执行栈链接警告；未引入第三方依赖，未创建 Git 提交。
