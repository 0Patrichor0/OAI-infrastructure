# OAI infrastructure：可复用 5G 意图控制实验床

保存当前实验床的部署配置、定制源码、上游补丁和启动方式，便于在新机器上重建。采集日期：2026-10-30。

当前默认部署方案为 **单台 Linux 主机上的 OAI 5GC + CU-CP/CU-UP/DU + RFsim UE + FlexRIC/xApp + NWDAF + 意图控制前端**。真实手机、USRP B210 和跨主机 RAN 接入需要另外配置，尚未完成端到端验收。

```text
网页 / 意图 CLI ──→ 解析、预检、执行、回读、回滚
                       ├─→ 5GC 订阅 / QoS / 切片配置
                       └─→ 控制 xApp → FlexRIC → E2 → DU 调度器
                                                   ↑
                  NWDAF / KPM / 会话与运行统计 ─────┘
```

| 内容 | 位置 |
| --- | --- |
| 意图后端、页面、E2 控制 xApp 源码 | `intent-driven/` |
| 已修改的部署配置与启动/恢复脚本 | `overlays/` |
| OAI、FlexRIC、NWDAF、AnLF 的源码差异 | `patches/` |
| systemd 服务、定时器和覆盖配置 | `systemd/` |
| 上游精确提交、镜像清单、服务启用状态 | `*.lock.json` |
| 获取源码、生成配置、构建、安装和检查 | `scripts/` |
| 隔离回归测试 | `tests/`、`intent-driven/tests/` |

上游大型源码按固定提交获取，不重复上传；二进制、运行数据库、历史日志、真实 SIM K/OPc、SSH/WireGuard 私钥和模型 API key 不包含在仓库中。首次渲染自动生成新的实验凭据，RFsim UE 与核心网种子订阅保持一致。

新机器部署请按 [完整复用指南](docs/REUSE.md) 操作。原有运行机器请先备份，不要把初始化步骤直接用于现有数据库。

```bash
git clone https://github.com/0Patrichor0/OAI-infrastructure.git
cd OAI-infrastructure
sudo bash scripts/install_deps.sh
# 完成指南中的 Docker 权限与服务用户 sudo 前置条件后：
python3 scripts/fetch_sources.py --home "$HOME"
python3 scripts/render.py --home "$HOME"
python3 scripts/stage.py
bash scripts/build_asn1c.sh "$HOME"
python3 scripts/build.py --home "$HOME" --jobs 3
sudo python3 scripts/install_profile.py
python3 scripts/start.py --home "$HOME"
```

默认页面只监听本机 `127.0.0.1:8099`，远程访问可在电脑执行：

```bash
ssh -L 8099:127.0.0.1:8099 ubuntu@YOUR_SERVER_IP
```

然后打开 `http://localhost:8099`。自然语言模型为可选项，确定性意图解析不要求模型 key。

开发检查：

```bash
python3 -m venv .local/venv
. .local/venv/bin/activate
pip install -r requirements-dev.txt
python scripts/check.py
```

检查需要 Linux、Bash、GCC、Node.js 和 Python。包含配置渲染、后端/页面回归、DU 策略 C 单元检查及敏感信息扫描，不会下发意图或启动实际网络。

能力边界与测试证据见 [架构说明](docs/ARCHITECTURE.md) 和 [验证记录](docs/VALIDATION.md)。第三方代码继续遵循其各自许可证，见 [许可证说明](THIRD_PARTY_NOTICES.md)。
