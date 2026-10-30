# 意图经 xApp、FlexRIC、E2 控制 DU 下行 MCS

部署日期：2026-10-29。服务器：YOUR_SERVER_IP。

第一条真实 RAN 写控制链路已实现：

```text
自然语言意图 / 前端表单
  → set_ran_dl_mcs（CLI 账本、快照、回滚）
  → xapp_mcs_control（E42 控制 xApp）
  → FlexRIC near-RT RIC
  → E2 RIC CONTROL，定制 MAC SM 142
  → DU E2 agent 回调
  → 在 MAC 调度锁内修改 cells[0].dl_bler.max_mcs
  → 新鲜回读 + E2 MAC 遥测 + 用户面通信验证
```

控制阶段不修改 CU/DU 配置文件、不重启服务。首次安装新二进制与 MAC 服务模型时执行了一次服务重启；随后热更新与回滚期间进程保持不变。

## 如何使用

刷新前端 `http://YOUR_SERVER_IP:8099/`。页面新增“DU 无线控制 · E2”，可读取当前值、填写下行 MCS 上限并下发；任务会出现在意图账本中，可按先后顺序逆序回滚。

自然语言示例：

```text
将 DU 下行 MCS 上限设为 20
```

对应动作：

```json
{"action":"set_ran_dl_mcs","target":{},"params":{"max_mcs":20}}
```

接口：`GET /api/ran/mcs` 回读，`POST /api/ran/mcs` 下发 `{"max_mcs":20}`。自然语言也可通过原有 `/api/parse`、`/api/apply` 入口执行。

## 实际观测

| 阶段 | DU 运行态上限 | MAC 遥测最高回报 | DU PID |
|---|---:|---:|---:|
| 原始状态 | 28 | 28 | 1087123 |
| 自然语言设置为 20 | 20 | 20 | 1087123 |
| 表单 API 设置为 18 | 18 | 18 | 1087123 |
| 回滚后一条意图 | 20 | 回读确认恢复 20 | 1087123 |
| 回滚第一条意图 | 28 | 28 | 1087123 |

每次用于独立确认的查询收到 150 次 E2 MAC 指示、450 条 UE 观测样本。主 UE、UE2、UE3 的用户面 ping 均有回应。

参与测试的意图：`261029115456-ab3ec3`、`261029115515-d8df26`，均完成回滚，无遗留测试意图。

RIC、CU-CP、CU-UP、DU、主 UE、UE2、UE3 的 PID 在热更新/回滚测试前后完全一致。测试先故意请求乱序回滚，观察到被拒绝且上限仍为 18；随后逆序回滚恢复 20、28。两个并发只读查询均返回各自匹配请求编号的回执。

## 如何避免“收到 ACK 就算成功”

当前 FlexRIC 同步控制接口不向调用者提供完整应用结果，E2 ACK 只能证明控制过程得到响应。因此后端额外要求 DU 生成执行回执：

- 回执由 DU 在读取真实调度字段后生成，并带请求编号、操作类型、DU PID、时间戳及策略版本。
- `/run/oran-du-e2-control/state.json` 位于 root 管理的运行目录，是只读执行证据；后端不通过写该文件控制 DU。
- 后端核对文件权限、请求编号、新鲜度和运行进程，再经 xApp 查询一次真实上限，并读取 E2 MAC 遥测。
- 缺失遥测、旧回执、错误 PID、未匹配的参数均不能标记意图达成。
- 下发新协议前检查当前运行的 DU 二进制是否含配套扩展，避免向旧演示版 agent 发送会触发其断言的命令。
- 策略版本和本地归属记录阻止旧意图覆盖后续控制；读请求和控制请求共用专用互斥锁，避免回执相互覆盖。

## 能力范围

- 这是 FlexRIC **定制 MAC 服务模型 142** 的部署内扩展，尚不是标准 E2SM-RC 控制实现。
- 当前仅支持一个 DU、cell_index=0；作用于小区全部 UE，不支持单 IMSI 目标。连接多个可选 DU 时 xApp 拒绝歧义选择。
- 支持下行上限 0–28，并且不得低于 DU 已配置的最小 MCS；不支持上行 MCS 控制。
- 改动影响新下行调度，既有 HARQ 重传保留其参数。MAC 观测来自现有调度统计，不是逐个空口 PDSCH 的抓包验证。
- 这是运行时策略；DU 重启后恢复启动配置，必须重新下发。旧进程快照不能跨重启直接回滚。
- 此次没有实现 CU 控制、切片 PRB 配额、无线资源隔离或吞吐/SLA 保证。

## 验证与文件位置

原有 Python 回归 24 项、新增控制回归 9 项均通过。生产策略头文件的独立 CTest 和 FlexRIC `Unit_test_MAC_SM` 均通过。修改的 MAC SM 文件在 E2AP_V1/KPM_V2_01、E2AP_V2/KPM_V2_03、E2AP_V3/KPM_V3_00 组合下完成语法检查；实际部署和端到端观测为 E2AP_V3/KPM_V3_00。

前端 JavaScript 逻辑测试 5 组通过，包含新增表单的整数传参、非整数拒绝和回读展示；未进行浏览器点击及视觉验收。

OAI 原有构建目录 `ctest -N` 显示 0 个测试，未将此作为测试通过。新增生产策略测试独立注册在 `intent-driven/e2-tests/`。

服务器代码：

- `/home/ubuntu/intent-driven/intentlib/actions_ran.py`
- `/home/ubuntu/intent-driven/intentlib/ran_control.py`
- `/home/ubuntu/intent-driven/ran/xapp_mcs_control.c`
- `/home/ubuntu/oai-ran-e2/openair2/E2AP/RAN_FUNCTION/CUSTOMIZED/ran_func_mac.c`
- `/home/ubuntu/oai-ran-e2/openair2/E2AP/RAN_FUNCTION/CUSTOMIZED/intent_mcs_policy.h`
- FlexRIC `src/sm/mac_sm/mac_sm_agent.c`、`mac_sm_ric.c`

新增服务 drop-in：`/etc/systemd/system/oran-du.service.d/30-e2-control.conf`。

备份：`/home/ubuntu/agent-repair-e2-mcs-20261029-113902/`、`/home/ubuntu/e2-mcs-deployment-20261029-115039/`。已有 NAS 和 RIC 修复改动被保留，未创建 git 提交。

复跑新增回归：

```bash
python3 /home/ubuntu/intent-driven/e2-tests/test_mcs_backend.py --root /home/ubuntu/intent-driven
cmake -S /home/ubuntu/intent-driven/e2-tests -B /home/ubuntu/intent-e2-tests-build
cmake --build /home/ubuntu/intent-e2-tests-build
ctest --test-dir /home/ubuntu/intent-e2-tests-build --output-on-failure
```

[实际下发记录](live-tests.json) · [首次部署与回读](deployment.json)
