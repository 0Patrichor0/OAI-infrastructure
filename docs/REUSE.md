# 在新机器上重建实验床

这份指南面向新的专用实验主机。默认恢复服务器 RFsim 方案，不导入原机器的运行数据库或真实 SIM 密钥。重新部署后会有新的鉴权凭据与新的运行状态。

## 1. 前置条件

- 原部署环境为 x86-64 Linux；构建配置使用 GCC 13、AVX2、E2AP v3、KPM v3 和指定版本 ASN1C。新部署以 Ubuntu 24.04 为目标；其他发行版需要核对包名和工具链。
- 本地源码路径使用无空格的 Linux 绝对路径，服务用户为普通用户。以下以该用户的 `$HOME` 为部署根目录；不要用 root 运行获取、渲染和暂存步骤。
- 使用具有 systemd、Docker、SCTP、网络命名空间与 TUN 支持的专用主机。内部 Docker 网络占用 `192.168.70.0/24`、`192.168.72.0/24`、`192.168.73.0/24`、`192.168.74.0/24` 等地址，UE 地址使用 `12.1.1.0/24`；部署前检查是否与现有网络冲突。
- 运行构建与 Docker 拉取需要访问 GitHub、EURECOM GitLab、Docker 镜像源及语言包仓库；镜像摘要固定并不保证上游永久保留镜像。
- 默认构建并发为 3；完整 OAI/NWDAF 构建需要预留磁盘与内存。

安装依赖后，为部署用户配置 Docker 权限，例如 `sudo usermod -aG docker "$USER"`，重新登录后确认 `docker info` 可成功执行。Docker 组具有主机管理权限。

当前意图后端会通过 `sudo -n` 执行 Docker、systemd、网络操作。沿用原实验床的权限模型，需要在专用测试主机为服务用户配置相应的非交互 sudo 权限；安装器不会修改 sudoers。若采用受限 sudoers，需按 `intent-driven/intentlib/` 的实际调用逐项授权，并用 `visudo` 校验。未满足时，页面读取可能正常而执行操作失败。

## 2. 获取固定版本与补丁

```bash
git clone https://github.com/0Patrichor0/OAI-infrastructure.git
cd OAI-infrastructure
sudo bash scripts/install_deps.sh
python3 scripts/fetch_sources.py --home "$HOME"
```

`upstreams.lock.json` 固定 NWDAF、AnLF、核心网部署库、OAI RAN、FlexRIC 和 ASN1C 提交。脚本为独立目录建立 Git 仓库，再应用本仓库中的补丁；不对已有目录执行强制重置。若目录已有内容、版本不符或补丁冲突，请改用干净的部署用户/目录。

## 3. 生成本机配置与实验订阅

```bash
cp .env.example .env
# 如需自然语言模型，编辑 .env 中 INTENT_LLM_*；其余留空可自动生成。
python3 scripts/render.py --home "$HOME"
python3 scripts/stage.py
```

生成的 `.env`、`.local/rendered/`、暂存后的 UE 配置、数据库种子包含新凭据，请留在本机。`.env` 采用单行 `KEY=value`；自动生成的密码为十六进制。重复渲染保留已有凭据；输出目录非空时脚本拒绝覆盖，可用 `--output .local/rendered-v2` 创建下一版，并向 `stage.py` / `install_profile.py` 传入同一个 `--rendered`。

默认生成四个 RFsim UE 的 AuthenticationSubscription 和 SessionManagementSubscriptionData，使用配置中的 IMSI/SST/SD；保留可用的 QoS 模板。SQL 在新建 MySQL 数据目录时初始化。它会清空这两张实验订阅表再加入四个 UE，**不要向现有业务数据库手工导入这个文件**。已有 MySQL 数据卷不会重新执行初始化脚本；改变 `.env` 也不会自动改变已有数据库用户密码。

`stage.py` 将生成文件放到目标用户目录，在 `.local/oai-stage-backups/` 中保存被覆盖文件；并将可拉取的镜像标签替换为捕获的摘要。本地构建的 AnLF/MTLF 镜像继续使用标签。`images.lock.json` 是采集清单，不是已上传的镜像备份。

暂存必须在编译前完成，因为它也提供 OAI 策略头文件和控制 xApp 源码。补丁先应用，覆盖文件后暂存。

## 4. 编译与安装

```bash
bash scripts/build_asn1c.sh "$HOME"
python3 scripts/build.py --home "$HOME" --jobs 3
sudo python3 scripts/install_profile.py
```

构建器直接调用 CMake 与 Docker，编译 FlexRIC、CU/DU/UE、模块和 MCS/PRB 控制 xApp，再构建 AnLF/MTLF。不要用上游遗留 `anlf_build_first.sh` 代替此流程；它包含广泛的 Docker 清理和防火墙操作。

安装器复制 systemd 单元、`/usr/local/bin` 辅助脚本及 FlexRIC 动态库，并启用采集时的服务和定时器。它本身不启动或停止实验床。服务覆盖配置来自 `systemd/`；`intent-driven/systemd/` 是较早的开发参考副本。

## 5. 启动与验收

```bash
python3 scripts/start.py --home "$HOME"
# 后续日常拉起仍可运行原入口：
~/nwdaf-rel17/start-all.sh
# 安装完成后，只读检查：
python3 scripts/start.py --home "$HOME" --check
```

首次启动除了执行 `start-all.sh`，还启动 UI 和启用的自动化定时器。原启动脚本会检查核心网健康/N4关联，必要时重建 UPF/重启 SMF，再启动 FlexRIC、CU、DU、UE、流量与监测 xApp，并执行恢复检查。请查看完整输出；末尾有警告时不能仅凭进程在运行判定验收通过。

建议依次确认：Docker 核心网健康、SMF 与 UPF 的 N4 关联、CU 的 NG/F1/E1 连接、UE 注册/PDU 地址及业务 ping、FlexRIC E2 节点、KPM 数据、NWDAF 事件增长。再在页面读取 MCS/PRB 当前值，记录基线后测试下发与回滚；E2 ACK 本身不能证明调度器已生效。

默认页面监听 `127.0.0.1:8099`。使用 README 中的 SSH 转发访问；重启后 systemd 会恢复已启用服务，日常完整检查仍用 `start-all.sh`。停止实验可使用 `~/nwdaf-rel17/stop-all.sh`，并根据需要停止定时器，防止自动修复重新拉起服务。

常用定位入口：

```bash
systemctl status intent-ui flexric-ric oran-cu-cp oran-cu-up oran-du
journalctl -u intent-ui -n 100 --no-pager
docker logs --tail 100 oai-smf
ls ~/oai-ran-logs
```

## 6. 现有实验机更新与跨主机部署

这是源码复建包，不是整机备份。原机上的订阅、策略状态、审计、模型与数据库卷没有上传；更新前应单独备份这些内容。`stage.py` 会覆盖目标配置，不能替代带状态的升级或数据库迁移。

若将 RAN 放到本机、核心网留在云端，需另配隧道/路由、NGAP SCTP、GTP-U 双向可达及 MTU，并修改 RAN 通告地址和控制后端执行位置。尤其注意 VPP UPF 的 N3 地址为 `192.168.72.201`，管理地址不能直接替代 N3。当前 UI 默认仍在同一主机执行 RAN 控制；本仓库不自动改造成跨主机控制。

接 B210/手机还需要匹配的射频参数、频段、SIM 鉴权和本地 UHD 数据流。此前 WSL USB 持续采样存在丢包，真实手机尚未完成入网验证；不能把 RFsim 的验收结果用于宣称射频接入成功。
