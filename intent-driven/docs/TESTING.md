# 意图驱动 5G 运维 · 完整测试流程(照着敲)

适用环境:本机 OAI 5GC + O-RAN(FlexRIC + CU/DU/UE)+ NWDAF,意图控制面在
`/home/ubuntu/intent-driven`。

每一步固定结构:**目的 → 命令 → 预期现象 → 判据 → 说明了什么 → 失败怎么办**。
不满足判据不要往下走 —— 后面的验证会拿到空结果,而且看不出是"能力不支持"还是"前置没就绪"。

全流程约 **25~35 分钟**,其中阶段 7、8、10 会重建 UPF/SMF 或整栈,是"高代价阶段"。

数值都是本机实测参考值,重跑会变,**看量级不看绝对值**。

---

## 总览:每个阶段验证什么

| 阶段 | 意图 | 动作 | 耗时 | 影响 | 验证的核心能力 |
|---|---|---|---|---|---|
| 0 | — | 环境自检 | 2 min | 无 | 数据面/控制面/采集面都在 |
| 1 | 解析任意自然语言 | `parse` | 秒级 | 无 | NL → 结构化意图,可审计 |
| 2 | 新增用户 | `add_subscriber` | 秒级 | 无 | 签约写入 + 幂等收敛 |
| 3 | 修改 QoS | `set_qos` | ~10 s | 断该 UE 会话 | 会话重建生效 + 回读校验 |
| 4 | 开通新业务/切片 | `provision_service` | ~10 s | 断该 UE 会话 | S-NSSAI 档案下发 |
| 5 | 降低时延 | `reduce_latency` | ~10 s | 断该 UE 会话 | 5QI 档位 + 实测 RTT |
| 6 | 保障 SLA | `sla_guard` | 秒级 / ~10 s | 达标时无影响 | KPI 判定 + 违约补偿 |
| 7 | 增加容量 | `boost_capacity` | 2~4 min | **断所有会话** | UPF worker + SMF 重关联 |
| 8 | 调整数据出口 | `set_egress` | 2~4 min | **断所有会话** | N6 切换 + 流量走向证据 |
| 9 | 回滚 | `rollback` | 视动作 | 视动作 | 快照恢复 + 顺序依赖 |
| 10 | 持久化 | `persist` + L3 重建 | 5~8 min | **整栈重建** | 意图结果跨重建存活 |
| 11 | 前端/账本 | UI + `replay`/`verify` | 2 min | 无 | 可操作、可回放、可复验 |

---

## 阶段 0:环境自检(不通过就别往下走)

### 0.1 定义两个查询助手(后面一直用)

```bash
cd /home/ubuntu/intent-driven
dbq() { sudo docker exec mysql mysql -uroot -p -N -B oai_db -e "$1"; }
kpm() {   # 自动挑「有 KPM 数据」的库,并打印用的是哪个
  local f n
  for f in $(ls -t /tmp/xapp_db* 2>/dev/null | grep -v -- '-wal$\|-shm$'); do
    n=$(sqlite3 "file:$f?mode=ro" 'select count(*) from KPM_MeasRecord;' 2>/dev/null || echo 0)
    [ "${n:-0}" -gt 0 ] && break
  done
  echo "# kpm 库: $(basename "$f") ($n 行)" >&2
  sqlite3 "file:$f?mode=ro" "$1"
}
```

> `dbq` 走容器内 mysql(3306 未对外发布)。
> `kpm` 读 FlexRIC 的 KPI 库 —— **两个 xApp 各写一个库**:`xapp_kpm_moni` 写 `KPM_MeasRecord`,
> `xapp_gtp_mac_rlc_pdcp_moni` 写 `MAC_UE`/`RLC_bearer`。按 mtime 取"最新"会挑错库,
> 所以按"表里有数据"来选。

### 0.2 服务与容器

```bash
systemctl is-active flexric-ric oran-cu-cp oran-cu-up oran-du oran-ue oran-traffic \
  flexric-xapp-kpm flexric-xapp-gtp-mac intent-ui | paste -sd' '
sudo docker ps -q | wc -l
```

**预期**:`active` 出现 9 次;容器 25 个左右。
**判据**:没有 `inactive`/`failed`;容器数 ≥ 24。
**说明**:RAN 五个单元(ric/cu-cp/cu-up/du/ue)+ 流量生成 + 两个 xApp + 前端都在。
**失败**:`bash /home/ubuntu/nwdaf-rel17/start-all.sh`(不带 `--force`)。

### 0.3 用户面(最关键的基线)

```bash
ip -4 addr show oaitun_ue1 | grep inet
ping -c 3 -W 2 -I oaitun_ue1 192.168.73.135 | tail -2
```

**预期**:
```
inet 12.1.1.2/24 scope global oaitun_ue1
3 packets transmitted, 3 received, 0% packet loss
rtt min/avg/max/mdev = 6.2/6.9/7.5/0.6 ms
```
**判据**:有 `12.1.1.x` 且 0% 丢包。
**说明**:UE 会话在、N3/N6 通、UPF 转发正常 —— 后面所有"会话重建"的动作才有意义。
**失败**:`sudo systemctl restart oran-ue`;仍不行查 `tail -30 ~/oai-ran-logs/ue.log`。

### 0.4 活动 UE 的 IMSI

```bash
systemctl cat oran-ue.service | grep -oP 'IMSI\s*\K\d{15}'
```

**预期**:`208950000000031`。
**判据**:15 位数字。
**说明**:这是"改 QoS 会断会话"的那个目标;动作内部也用它判断是否需要重建会话。
**失败**:改 `oran-ue.service` 的 Description 或直接看 `ue.conf` 的 `imsi`。

### 0.5 NWDAF 采集在增长

```bash
for i in 1 2 3; do
  sudo docker exec oai-nwdaf-database mongosh --quiet testing \
    --eval 'print("qosmon:",(db.smf.findOne()||{}).qosmonlist ? db.smf.findOne().qosmonlist.length : 0)'
  sleep 5
done
```

**预期**:`qosmon: 120` → `125` → `130`。
**判据**:三次数字递增。
**说明**:SMF 的 QoS 监控事件正在被 SBI 收进 Mongo —— 阶段 6 的 SLA 判定就靠它。
**失败**:`sudo docker logs --since 2m oai-nwdaf-sbi 2>&1 | grep -c "Storing SMF notification"`
为 0 时:`sudo systemctl start oai-nf-watchdog.service`(它会重启 SBI 恢复订阅)。

### 0.6 KPM 在上报

```bash
kpm "select meas_name, count(*), round(avg(measure_value),3)
     from KPM_MeasRecord
     where meas_name like 'DRB.%'
       and tstamp > (cast(strftime('%s','now') as integer)-60)
     group by meas_name;"
```

**预期**:
```
DRB.RlcSduDelayDl|112|0.0
DRB.UEThpDl|112|0.014
DRB.UEThpUl|112|0.029
```
（`CARR.*` 那些是 MCS 分布,一分钟能写几万行,不要拿它判断 liveness）
**判据**:有行返回,且出现 `DRB.*` 系列。
**说明**:RAN 侧 KPI 落库正常。两个坑要记住:
- **时间戳单位不统一**:KPM 库的 `tstamp` 是 epoch **秒**,MAC/RLC 库是**微秒** —— 按微秒过滤 KPM 会永远查到 0 行(看起来像没数据)。
- **WAL 模式**:只读连接可能看不到刚写入的行,所以判据用"有行 + 数值量级合理",别用"最后一秒一定有"。
- 本环境 KPI 量级很小(rfsim + 轻流量):`DRB.UEThpDl` 均值约 `0.01`、峰值 `0.75`,`DRB.RlcSduDelayDl` 常为 `0.0`,别拿 50 Mbps 当阈值。

---

## 阶段 1:意图解析(只看,不执行)

### 1.1 解析一条改 QoS 的意图

```bash
./intentctl.py parse "把 208950000000031 的 5QI 改成 9，上下行各 200Mbps" --no-llm
```

**预期现象**:
```
结构化意图
  action      : set_qos
  target.imsi : 208950000000031
  params      : {"fiveQI": 9, "ambr_ul": "200Mbps", "ambr_dl": "200Mbps"}
  constraints : {"allow_session_rebuild": true}
  parser      : rules / regex-v1 / 置信度 0.95
  依据        : 规则解析:set_qos / imsi=... / {...}
```
**判据**:`action` 正确、`fiveQI=9`、`ambr_ul`/`ambr_dl` 都为 `200Mbps`、置信度 ≥ 0.8。
**说明**:自然语言被解析成**可审计的结构化意图** —— 这是"意图驱动"和"脚本参数"的区别:解析结果本身就是一条记录,能回放。
**失败**:若 `fiveQI` 缺失,说明动词间隔太长;解析器会打 `⚠ 提到 5QI 但没解析出数值` 并把置信度降下来(这是设计行为,不静默丢字段)。

### 1.2 解析一条不该串动作的意图(回归用例)

```bash
./intentctl.py parse "给新用户 IMSI 208950000000098 开通，5QI 6，下行 100Mbps，切片 SST 1" --no-llm
```

**预期**:`action: add_subscriber`(不是 `provision_service`)。
**判据**:`action` 必须是 `add_subscriber`。
**说明**:「新用户 + 切片参数」是"给订户配切片档案",不是"开通新切片业务"。这条曾经解析错,现在锁死优先级。

---

## 阶段 2:新增用户(不打断任何会话)

> 用 `208950000000098`:它在本机库里**有鉴权行、没有签约行**,正好演示"幂等收敛"。

### 2.1 先确认现状

```bash
dbq "select 'auth', count(*) from AuthenticationSubscription where ueid='208950000000098'
     union all select 'sm', count(*) from SessionManagementSubscriptionData where ueid='208950000000098';"
```

**预期**:`auth 1` / `sm 0`。
**说明**:真实环境常见"半开通"状态,意图必须能收敛,而不是报错。

### 2.2 执行意图

```bash
./intentctl.py apply --nl "给新用户 IMSI 208950000000098 开通，5QI 9，上下行各 50Mbps，切片 SST 1" \
  --no-llm --yes
```

**预期现象**:
```
执行计划
  1. 识别 208950000000098 现状(鉴权/签约是否已存在)
  2. 208950000000098 已有部分数据 → 只补缺失的表,再按意图覆盖字段
  ...
验证结果
  ✓ AuthenticationSubscription 有 ... 的鉴权数据  复用原有
  ✓ SessionManagementSubscriptionData 有 ... 的签约数据
  ✓ 5QI = 9  实际 9
  ✓ 上行 AMBR = 50Mbps  实际 50Mbps
  ✓ 下行 AMBR = 50Mbps  实际 50Mbps
  ✓ SST = 1  实际 1
  ✓ 鉴权密钥非空(K/OPc)
意图达成 — 261026164040-xxxxxx
```
**判据**:全部 ✓,且打出意图 ID。
**说明**:动作是**收敛**而非"新建":复用了已有鉴权密钥,只补签约 —— 这是幂等设计。同时账本新增一条 `verified`,审计流水记了 parse/snapshot/executed/verified 四个 phase。

### 2.3 独立回读(不信动作自己的话)

```bash
dbq "select ueid,
  JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'\$.\"default\".\"5gQosProfile\".\"5qi\"')),
  JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'\$.\"default\".\"sessionAmbr\".\"uplink\"')),
  JSON_UNQUOTE(JSON_EXTRACT(singleNssai,'\$.sst'))
from SessionManagementSubscriptionData where ueid='208950000000098';"
```

**预期**:`208950000000098  9  50Mbps  1`。
**判据**:四个值与意图一致。
**说明**:验证证据来自**数据库回读**,不是动作自报。

### 2.4 确认没打断现有业务

```bash
ip -4 addr show oaitun_ue1 | grep inet
ping -c 2 -W 2 -I oaitun_ue1 192.168.73.135 | tail -1
```

**预期**:IP 不变(仍是 `12.1.1.2`),ping 正常。
**说明**:新增订户不触碰现有会话 —— 这就是意图计划里"代价:不打断现有会话"的实证。

---

## 阶段 3:修改 QoS(断该 UE 会话 ~10 秒)

### 3.1 先看执行计划,不动手

```bash
./intentctl.py apply --nl "把 208950000000031 的 5QI 改成 9，上下行各 200Mbps" --no-llm --dry-run
```

**预期现象**:计划里明确写着
```
1. 快照 208950000000031 当前 QoS 配置(可回滚)
2. 5QI → 9
3. 上行 AMBR → 200Mbps
4. 下行 AMBR → 200Mbps
5. 目标是当前活动 UE:重建 PDU 会话使其生效(断几十秒,oran-ue-watchdog 兜底)
--dry-run:未执行任何变更
```
**判据**:出现"重建 PDU 会话"这一步,dry-run 没有改库。
**说明**:意图在**执行前就声明代价** —— 这是可审批的前提。

### 3.2 执行

```bash
./intentctl.py apply --nl "把 208950000000031 的 5QI 改成 9，上下行各 200Mbps" --no-llm --yes
```

**预期现象**:
```
✓ 签约数据仍可读
✓ 5QI = 9  实际 9
✓ 上行 AMBR = 200Mbps  实际 200Mbps
✓ 下行 AMBR = 200Mbps  实际 200Mbps
✓ UE 已重建 PDU 会话  等 9s, IP 12.1.1.3, [NAS] Received PDU Session Establishment Accept, UE IPv4: 12.1.1.3
✓ 用户面可达 ext-dn 192.168.73.135
```
**判据**:**五类证据齐全** —— DB 回读、会话重建证据(NAS 报文行 + 新 IP)、用户面可达。
**说明**:OAI 在**会话建立时**读签约数据,所以改 QoS 必须重建会话才生效;9 秒是本机实测的重建耗时。注意 IP 可能从 `.2` 变 `.3`(会话重分配),这是正常的。

### 3.3 独立核对会话确实重建了

```bash
grep -a "PDU Session Establishment Accept" ~/oai-ran-logs/ue.log | tail -2
```

**预期**:最后两行时间戳不同、IPv4 相同或变化。
**说明**:日志是原始证据,不是动作的转述。

---

## 阶段 4:开通新业务 / 新切片档案(断会话 ~10 秒)

### 4.1 执行

```bash
./intentctl.py apply --nl "给 208950000000031 开通新业务，切片 SST 2 SD abcdef，5QI 6，下行 100Mbps" \
  --no-llm --yes
```

**预期现象**:计划里除 QoS 外还有一行
```
✓ SST = 2  实际 2
✓ SD = abcdef  实际 abcdef
✓ UE 已按新业务档案重建会话  9s, IP 12.1.1.4
```
并且计划里带一条坦白说明:
```
说明:UPF 的 SNSSAI_SST/SD 仍是旧值;本环境 SMF 不做 NSSAI 强校验,
     要真隔离需重建 UPF + 改 DU 切片配置(不在本动作范围)
```
**判据**:SST/SD 回读一致 + 会话重建成功。
**说明**:**这就是本环境的真实边界** —— 核心侧业务档案可以下发并生效,但 RAN 侧切片隔离不可执行:
`defn.rc.ctrl=NULL`(CU-CP)、DU 全空、`write_ctrl_rc_sm()` 只打 printf。所以不能承诺"切片隔离",只能承诺"业务档案 + 会话生效"。

### 4.2 核对 UPF 的 S-NSSAI(解释为什么不能承诺隔离)

```bash
sudo docker inspect vpp-upf --format '{{range .Config.Env}}{{println .}}{{end}}' | grep SNSSAI
```

**预期**:`SNSSAI_SST=1` / `SNSSAI_SD=ffffff`(与订阅里的 2/abcdef 不一致)。
**说明**:UE 仍能建会话,说明 SMF 没做强校验;要真隔离必须改 UPF 并重建(断所有会话),不属于本动作。

---

## 阶段 5:降低时延(断会话 ~10 秒)

### 5.1 记录基线 RTT

```bash
ping -c 20 -i 0.2 -W 2 -I oaitun_ue1 192.168.73.135 | tail -2
```

**预期**:`rtt min/avg/max/mdev = 6.0/9.1/13.8/... ms`(记住 avg)。
**说明**:没有基线就没法判断"降时延"是否真的发生。

### 5.2 执行

```bash
./intentctl.py apply --nl "把 208950000000031 的时延降下来" --no-llm --yes
```

**预期现象**:
```
1. 记录基线:RTT 20 次采样 + KPM DRB.RlcSduDelayDl
2. 5QI 6 → 7(低时延档)
3. 重建 PDU 会话生效(断 ~10s)
4. RAN 侧调度/TC 不可控(RC 控制面在本架构是空壳),本动作只走核心侧 QoS 档位
✓ 5QI = 7
✓ UE 会话已重建  9s
✓ RTT 采样完成  avg 9.101ms / max 13.862ms
```
**判据**:5QI 生效 + 会话重建 + RTT 有实测值。
**说明**:时延改善**不保证** —— RTT 主要取决于 rfsim/宿主调度,5QI 在核心侧不改 RAN 调度。文档里写"实测值",不写"降低了 X%"。

---

## 阶段 6:保障 SLA(达标不动,违约补偿)

### 6.1 达标场景(不该有任何变更)

```bash
./intentctl.py apply --nl "保障 208950000000031 的 SLA，时延不高于 200ms" --no-llm --yes
```

**预期现象**:
```
✓ 基线采样 rtt  8.759
✓ 已达标(无需补偿):rtt 未越过 200.0  8.759
```
并且**没有**"补偿后会话已重建"这一行。
**判据**:两行 ✓,且 UE 未被重启(IP 不变)。
**说明**:SLA 意图是"收敛到目标",已达标时不动手 —— 这是"意图"与"定时脚本"的区别。

### 6.2 违约场景(会补偿,但可能仍达不到 —— 要如实报)

```bash
./intentctl.py apply --nl "保障 208950000000031 的 SLA，时延不高于 1ms" --no-llm --yes
```

**预期现象**:
```
✓ 基线采样 rtt  9.465
✓ 补偿后会话已重建  9s
✗ 复测 rtt 回到阈值内  9.465 → 8.494(阈值 1.0)
意图未完全达成
```
**判据**:**必须出现 ✗**,并且状态是 `failed`。
**说明**:这条用例验证的是**不撒谎** —— 补偿动作执行了、复测没达标,就如实报未达成。如果这里报了 `意图达成`,说明 SLA 判据写错了(吞吐是 `≥`、时延是 `≤`,方向写反会既误报又假成功)。

### 6.3 看 KPI 来源

```bash
kpm "select meas_name, round(avg(measure_value),3), count(*) from KPM_MeasRecord
     where meas_name in ('DRB.UEThpDl','DRB.RlcSduDelayDl')
       and tstamp > (strftime('%s','now')-120)*1000000 group by meas_name;"
```

**预期**:`DRB.UEThpDl` 均值约 `0.01`(峰值 <1)、`DRB.RlcSduDelayDl` 多为 `0.0`。
**说明**:SLA 的吞吐/时延判定来自这些 RAN 侧 KPI,不是拍脑袋的常量。

---

## 阶段 7:增加容量(高代价:断所有会话,2~4 分钟)

> 这一步会 `recreate vpp-upf` → **SMF 失去 PFCP 关联** → 动作会自动 `docker restart oai-smf` →
> 再重建 UE 会话。全程 2~4 分钟,UE 会掉线一次。

### 7.1 记录基线

```bash
grep -oP 'VPP_CORE_WORKER=\K[^\s#]+' \
  /home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose/docker-compose-basic-vpp-nrf.yaml
sudo docker exec vpp-upf sh -c 'sed -n "/cpu {/,/}/p" /openair-upf/etc/startup*.conf'
```

**预期**:compose 里 `1`;startup 里 `corelist-workers 1`。
**说明**:这个镜像的模板是 `corelist-workers $VPP_CORE_WORKER`,**变量是核列表不是数量** —— 写 `2` 只会得到一个 worker(core 2),要两个必须写 `1,2`。这是实测踩过的坑。

### 7.2 dry-run 看代价

```bash
./intentctl.py apply --nl "给 UPF 扩容到 2 个 worker" --no-llm --dry-run
```

**预期**:计划第 2 步写明"重建 vpp-upf(断所有会话)"。

### 7.3 执行

```bash
./intentctl.py apply --nl "给 UPF 扩容到 2 个 worker" --no-llm --yes
```

**预期现象**:
```
✓ UPF 环境 VPP_CORE_WORKER=1,2
✓ VPP startup worker 数 = 2  corelist-workers 1,2
✓ UE 会话已重建  8s, IP 12.1.1.2
✓ 用户面可达 ext-dn 192.168.73.135
✓ KPM 吞吐仍在采样  DRB.UEThpUl 近 60s 均值 63383(6 点)
意图达成 — ...
```
**判据**:四条 ✓,尤其是 `corelist-workers 1,2`(数量=2)。
**说明**:容量变更**落到容器真实配置**,并且恢复过程是自动的(UPF→SMF→UE 三段)。
**失败**:
- 只看到 `corelist-workers 2` → worker 仍是 1 个,变量语义用错了;
- `UE 会话已重建` 超时 → 查 `sudo docker logs --since 3m oai-smf 2>&1 | grep -i "UPF association"`,
  出现 `does not have a UPF association` 就说明 SMF 没重启(动作里应自动做)。

### 7.4 回滚

```bash
./intentctl.py rollback <上一行的意图ID>
```

**预期**:`worker 数已恢复 1;会话重建成功`。
**说明**:回滚读的是**快照里的旧值**,不依赖"我记得原来是几"。

---

## 阶段 8:调整数据出口(高代价:断所有会话,2~4 分钟)

### 8.1 记录基线

```bash
grep -oP 'IF_3_IP_REMOTE=\K[\d.]+' \
  /home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose/docker-compose-basic-vpp-nrf.yaml
ping -c 2 -W 2 -I oaitun_ue1 192.168.73.135 | tail -1
```

**预期**:`192.168.73.135`(ext-dn),ping 通。

### 8.2 执行

```bash
./intentctl.py apply --nl "把数据出口切到 192.168.73.136" --no-llm --yes
```

**预期现象**:
```
1. 确认/启动第二条 DN 容器 oai-ext-dn2(192.168.73.136)在 oai-public-core 上,带 12.1.1.0/24 回程路由
2. 改基础 compose:IF_3_IP_REMOTE 192.168.73.135 → 192.168.73.136(这样 start-all 重建也不会丢)
3. 重建 vpp-upf(代价:所有 UE 会话中断)
4. 重建 UE 会话
5. 验证:UE ping 192.168.73.136 通、ping 旧出口 192.168.73.135 不通(证明确实切走了)

✓ UPF 的 IF_3_IP_REMOTE = 192.168.73.136
✓ UE 会话已重建  10s, IP 12.1.1.2
✓ UE ping 新出口 192.168.73.136 通
✓ 流量确实进了新 DN(dn2 rx 增长)  dn2 rx +294B
✓ 旧 DN 未再收到这段流量(dn1 rx 未变)  dn1 rx +0B
```
**判据**:最后两条 —— **看的是 DN 网卡计数器,不是 ping 失败**。
**说明**:第二条 DN 是容器路由器,能把包转给旧 DN,所以"ping 旧出口不通"是**错误判据**(我第一次就栽在这)。正确证据是:新 DN 的 `rx` 字节增长、旧 DN 的 `rx` 不变。

### 8.3 独立复核

```bash
sudo docker exec oai-ext-dn2 sh -c "awk '/eth0:/ {print \$2}' /proc/net/dev"
sudo docker exec oai-ext-dn sh  -c "awk '/eth0:/ {print \$2}' /proc/net/dev"
sudo docker exec oai-ext-dn2 ping -c2 192.168.73.201 | tail -1   # 回程路由有效
```

### 8.4 回滚

```bash
./intentctl.py rollback <意图ID>
```

**预期**:`N6 下一跳已恢复 192.168.73.135;会话重建成功;已清理 oai-ext-dn2`。

---

## 阶段 9:回滚链路(证明"可回滚"不是口号)

回滚**必须按逆序**:后做的先回。

```bash
./intentctl.py list --limit 6
./intentctl.py rollback <最新ID>       # 后做的
./intentctl.py rollback <次新ID>
```

**预期**:每次回滚都打 `已恢复 ... ;会话重建成功(9s)`,账本状态变 `rolled_back`。
**判据**:回滚后 DB 回读回到**变更前**的值。
**说明**:快照是 `mysqldump --where` 的行级 SQL,回滚 = 先 `DELETE` 再回放(直接回放会撞主键)。

### 9.1 回滚后必做的基线核对

```bash
dbq "select ueid,
  JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'\$.\"default\".\"5gQosProfile\".\"5qi\"')),
  JSON_UNQUOTE(JSON_EXTRACT(singleNssai,'\$.sst')),
  JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'\$.\"default\".\"sessionAmbr\".\"uplink\"'))
from SessionManagementSubscriptionData where ueid='208950000000031';"
ip -4 addr show oaitun_ue1 | grep inet
ping -c 2 -W 2 -I oaitun_ue1 192.168.73.135 | tail -1
```

**预期**:`031 | 6 | 222 | 100Mbps`,UE 有 IP,ping 通。
**说明**:回到基线才叫"回滚完成";只看动作输出不算。

---

## 阶段 10:持久化(整栈重建后意图结果还在)

### 10.1 看持久层内容

```bash
./intentctl.py persist
```

**预期**:
```
已记录的 IMSI(4 个)
  208950000000031: auth ✓ / sm ✓
  208950000000096: auth ✓ / sm ✓
  208950000000097: auth ✓ / sm ✓
  208950000000099: auth ✓ / sm ✓
渲染: /home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose/database/zz_intent_state.sql  sha256=...
配置期望值(1 项 env / 0 个容器)
  VPP_CORE_WORKER=1,2 (vpp-upf)  ✓
```
**判据**:每个做过意图的 IMSI 都在,且 `zz_intent_state.sql` 有 hash。
**说明**:意图结果除了写库,还渲染成**幂等 SQL**(先删后插),供 mysql 初始化/对账重放。

### 10.2 先记基线,再整栈重建

```bash
dbq "select ueid, JSON_UNQUOTE(JSON_EXTRACT(singleNssai,'\$.sst')),
  JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'\$.\"default\".\"5gQosProfile\".\"5qi\"'))
from SessionManagementSubscriptionData order by ueid;" | tee /tmp/pre_l3.txt

bash /home/ubuntu/nwdaf-rel17/start-all.sh --force
```

**预期**:脚本走 5 个阶段,结尾自检全 ✓(`UE 会话 + 用户面正常`、`NWDAF 数据在增长`、`KPM 上报中`、`AI Fabric 有 N 个 ready 部署`、`闭环 Flask :8989 在跑`),约 4~6 分钟。

### 10.3 核对意图结果是否回来

```bash
dbq "select ueid, JSON_UNQUOTE(JSON_EXTRACT(singleNssai,'\$.sst')),
  JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'\$.\"default\".\"5gQosProfile\".\"5qi\"'))
from SessionManagementSubscriptionData order by ueid;" > /tmp/post_l3.txt
diff /tmp/pre_l3.txt /tmp/post_l3.txt && echo "完全一致 ✓"
```

**判据**:`diff` 无输出。
**说明**:重建会**新建 mysql 匿名卷 + 用种子 SQL 初始化**,意图结果本来会被冲掉;现在能逐字节回来。

### 10.4 看恢复是"哪条规则"触发的

```bash
sudo journalctl -u intent-state-apply --since '-15min' --no-pager | grep -E 'applied|unchanged|原因' | tail -5
```

**预期**:
```
applied b1291bd298aa（原因: mysql 容器重建过（可能被种子重置））
unchanged (b1291bd298aa)
```
**判据**:出现 `原因: mysql 容器重建过` 或 `库里缺记录`。
**说明**:对账有三条触发:**文件变化 / mysql 比上次应用新 / 持久层 IMSI 在库里缺失**。只看文件 hash 会漏掉重建场景(`applied.sha` 在磁盘上活着、DB 已被重置)—— 这是实测踩过的坑。

### 10.5 手工演练一次"缺记录"恢复(可选)

```bash
dbq "DELETE FROM SessionManagementSubscriptionData WHERE ueid='208950000000098';"
sudo systemctl start intent-state-apply.service
sudo journalctl -u intent-state-apply -n 3 --no-pager | grep -o 'applied.*'
dbq "select count(*) from SessionManagementSubscriptionData where ueid='208950000000098';"
```

**预期**:`applied ...（原因: 库里缺记录: 208950000000098）`,随后计数回到 1。

---

## 阶段 11:前端与账本(可操作、可回放、可复验)

### 11.1 页面

浏览器打开 `http://<机器IP>:8099`(或 `ssh -L 8099:127.0.0.1:8099 ubuntu@<host>`)。
**预期现象**:顶部状态条(UE IP / N4 心跳 / RAN 8 单元 / 容器数 / UPF worker / AI Fabric);
右栏「NWDAF 采集数据」显示 SMF/AMF 文档数、QoS 样本数、每 SUPI 末条用量、SBI 写入速率;
下方意图账本逐条列出状态。

### 11.2 用 API 验证(不点页面也能验)

```bash
curl -s http://127.0.0.1:8099/api/system | python3 -m json.tool | head -12
curl -s http://127.0.0.1:8099/api/nwdaf  | python3 -m json.tool | head -25
curl -s "http://127.0.0.1:8099/api/intents?limit=3" | python3 -m json.tool | head -12
```

**判据**:`/api/system` 里 `ue_ip` 非空、`ue_ping=true`;`/api/nwdaf` 里 `smf[0].qos > 0` 且 `age_s` 很小。

### 11.3 通过前端下发一条意图(走真实写路径)

```bash
curl -s -X POST http://127.0.0.1:8099/api/parse -H 'Content-Type: application/json' \
  -d '{"text":"保障 208950000000031 的 SLA，时延不高于 200ms"}' | python3 -m json.tool | head -20

JOB=$(curl -s -X POST http://127.0.0.1:8099/api/apply -H 'Content-Type: application/json' \
  -d '{"text":"保障 208950000000031 的 SLA，时延不高于 200ms"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -N --max-time 30 "http://127.0.0.1:8099/api/jobs/$JOB/stream" | tail -12
```

**预期**:`/api/parse` 返回结构化意图 + 计划;`/api/apply` 返回 `job id` + `status: running`;SSE 流最后出现 `意图达成 … [exit 0]`。
**说明**:前端**所有写操作都走 `intentctl.py` 子进程**,不复制业务逻辑 —— 账本/审计/回滚只有一套实现。

### 11.4 复用与复验

```bash
./intentctl.py verify <意图ID>          # 用账本里的执行结果重新验证
./intentctl.py replay <意图ID> --yes    # 用账本里的结构化意图重跑(不再走 NL 解析)
./intentctl.py show <意图ID>            # 看原文/解析/计划/快照/验证证据
```

**判据**:`verify` 全 ✓;`replay` 产生新意图且结果一致;`show` 能看到 `snapshot_json`。
**说明**:审计/复现的唯一入口是账本里的结构化意图,不是自然语言原文 —— 原文会歧义,结构化不会。

---

## 阶段 12:验收清单(全部满足即通过)

| # | 检查 | 命令 | 通过标准 |
|---|---|---|---|
| 1 | 环境自检 | 0.2~0.6 | 9 个 active、25 容器、UE ping 通、NWDAF 增长、KPM 有数据 |
| 2 | 解析可审计 | `parse` | action/params/置信度/parser 全有 |
| 3 | 新增用户幂等 | 阶段 2 | 复用鉴权、补签约、6 条 ✓、现有会话不断 |
| 4 | QoS 生效 | 阶段 3 | DB 回读一致 + NAS 重建证据 + 用户面可达 |
| 5 | 业务档案 | 阶段 4 | SST/SD 一致 + 会话重建;且明确 RAN 隔离不可承诺 |
| 6 | 时延档位 | 阶段 5 | 5QI 生效 + RTT 实测值 |
| 7 | SLA 不撒谎 | 阶段 6 | 达标不动;违约时如实 ✗ |
| 8 | 容量真实生效 | 阶段 7 | `corelist-workers 1,2` + 会话恢复 |
| 9 | 出口切换有证据 | 阶段 8 | dn2 rx 增长 + dn1 rx 不变 |
| 10 | 回滚到基线 | 阶段 9 | DB/UE/ping 均回基线 |
| 11 | 跨重建存活 | 阶段 10 | `diff pre/post` 为空 + journal 有 `原因: mysql 容器重建过` |
| 12 | 前端可操作 | 阶段 11 | API 有数据 + SSE 跑完 exit 0 + verify/replay 可用 |

---

## 附录 A:常用查询速查

```bash
# 意图账本
./intentctl.py list --limit 10
sqlite3 ledger.db "select id,action,state,created_at from intents order by created_at desc limit 5;"

# 审计流水(可回放的事实)
tail -5 audit/audit.jsonl | python3 -c "import sys,json;[print(json.loads(l)['phase'],json.loads(l)['intent_id']) for l in sys.stdin]"

# 签约
dbq "select ueid, JSON_UNQUOTE(JSON_EXTRACT(singleNssai,'\$.sst')) sst,
  JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'\$.\"default\".\"5gQosProfile\".\"5qi\"')) q,
  JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'\$.\"default\".\"sessionAmbr\".\"uplink\"')) ul
from SessionManagementSubscriptionData order by ueid;"

# 用户面
ip -4 addr show oaitun_ue1 | grep inet; ping -c3 -W2 -I oaitun_ue1 192.168.73.135 | tail -1

# NWDAF 采集
sudo docker exec oai-nwdaf-database mongosh --quiet testing --eval \
  'const d=db.smf.findOne(); print("qos:",(d.qosmonlist||[]).length,"sessions:",(d.pdusesestlist||[]).length)'

# KPM(注意 tstamp 单位:秒)
kpm "select meas_name,count(*),round(avg(measure_value),3) from KPM_MeasRecord
     where tstamp>(cast(strftime('%s','now') as integer)-120) group by meas_name;"

# MAC/RLC 库(另一个 xApp,时间戳是微秒)
gtp() { local f; f=$(ls -t /tmp/xapp_db* | grep -v -- '-wal$\|-shm$' | head -2 | tail -1)
        sqlite3 "file:$f?mode=ro" "select count(*) from MAC_UE;"; }

# 持久化对账
./intentctl.py persist
sudo journalctl -u intent-state-apply -n 5 --no-pager
```

---

## 附录 B:故障树

| 现象 | 先查 | 结论/处理 |
|---|---|---|
| `intentctl` 报 `mysql: ... Permission denied` 之类 | `ls -l state/applied.sha` | 被 root 写过 → `sudo chown ubuntu:ubuntu` |
| 解析出 `provision_service` 但你想加用户 | 文本里是否含"新用户/开通用户" | 含则必须走 `add_subscriber`;否则加"新业务/切片开通"字样 |
| `该 IMSI 没有签约数据,先做 add_subscriber` | `dbq` 查 sm 表 | `set_qos` 只改已有签约 |
| 会话重建 150s 超时 | `systemctl status oran-ue` / `ue.log` | UPF/SMF 刚重建时首轮常失败,动作会重试 3×90s;仍失败看 `ue.log` 的 `Bye.` |
| `PDU session does not have a UPF association` | 是否重建过 UPF | 必须 `docker restart oai-smf` 再重建 UE |
| 扩容后 `corelist-workers 2` 但只有 1 个 worker | 变量语义 | `VPP_CORE_WORKER` 是**核列表**,要 `1,2` |
| 出口切换后"ping 旧 DN 不通"判据失败 | DN 是容器路由器 | 改用 DN 网卡 `rx` 计数作判据 |
| `zz_intent_state.sql` 不生效 | `journalctl -u intent-state-apply` | 看是否 `unchanged`;hash 相同但库被重置 → 应命中"mysql 容器重建过"规则 |
| L3 后 KPM 不动 | `ss -a -A sctp \| grep 36421`、RIC 日志 `no registered nodes` | RIC 重启后 CU/DU 的 E2 agent 不会自动重注册 → 重启 `oran-cu-cp`→`oran-du`→`oran-ue`→两个 xApp |
| KPM 查询 0 行 | 助手挑到的是哪个库 | 两个 xApp 各写一个库;按 mtime 会挑到 gtp 的库。用文档里的 `kpm()` 按表选库 |
| KPM 有行但时间过滤后为空 | `select max(tstamp)` | KPM 库是**秒**,不是微秒;过滤式用 `-60` 而不是 `-60*1000000` |
| UI 打不开 | `systemctl status intent-ui`、`ss -ltnp \| grep 8099` | `sudo systemctl restart intent-ui` |
| 前端 NWDAF 卡住 | `curl /api/nwdaf` | 若 Mongo 查询超时,查 `docker exec oai-nwdaf-database mongosh --eval 'db.stats()'` |

---

## 附录 C:时间与影响速查

| 动作 | 会断什么 | 典型耗时 | 恢复方式 |
|---|---|---|---|
| `add_subscriber` | 无 | < 2 s | — |
| `set_qos` / `provision_service` / `reduce_latency` | 该 UE 的 PDU 会话 | ~10 s | 动作内自动重建 UE |
| `sla_guard`(达标) | 无 | < 3 s | — |
| `sla_guard`(违约) | 该 UE 会话 | ~10 s | 动作内补偿 + 重建 |
| `boost_capacity` / `set_egress` | **所有会话** | 2~4 min | UPF 重建 → SMF 重启 → UE 重建(动作自动) |
| `start-all.sh --force`(L3) | **整栈** | 4~6 min | 脚本自检 + 自愈 |

---

## 附录 D:收尾(把环境留成基线)

```bash
# 逆序回滚本次所有变更
./intentctl.py list --limit 10
./intentctl.py rollback <id_最新的>
# ... 依次回滚

# 核对基线
dbq "select ueid, JSON_UNQUOTE(JSON_EXTRACT(dnnConfigurations,'\$.\"default\".\"5gQosProfile\".\"5qi\"')) q,
  JSON_UNQUOTE(JSON_EXTRACT(singleNssai,'\$.sst')) sst from SessionManagementSubscriptionData where ueid='208950000000031';"
# 期望 6 / 222
grep -oP 'IF_3_IP_REMOTE=\K[\d.]+' /home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose/docker-compose-basic-vpp-nrf.yaml
# 期望 192.168.73.135
grep -oP 'VPP_CORE_WORKER=\K[^\s#]+' /home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose/docker-compose-basic-vpp-nrf.yaml
# 期望 1（如果之前做过扩容）
ip -4 addr show oaitun_ue1 | grep inet && ping -c2 -W2 -I oaitun_ue1 192.168.73.135 | tail -1
```


---

## 阶段 13:切片与 UE(新增/切换)

### 13.1 看切片清册

```bash
curl -s http://127.0.0.1:8099/api/slices | python3 -m json.tool | head -40
```

**预期**:每个切片四个勾:`ran_advertised` / `upf_registered` / `ue_requested` / `session_on_it`,
全勾才 `usable=true`。
**说明**:这是"切片是否真的可用"的唯一判据;四个面缺一个就会话失败。

### 13.2 新增 UE(只写签约,不打断会话)

```bash
curl -s -X POST http://127.0.0.1:8099/api/ues -H 'Content-Type: application/json' \
  -d '{"imsi":"208950000000099","sst":3,"sd":"000003","fiveQI":9,"ambr_ul":"80Mbps","ambr_dl":"80Mbps"}'
```

**判据**:job `done`,日志出现 6 条 ✓ + `意图达成`。
**说明**:只动 `oai_db`;UE 还在老切片上跑,不受影响。

### 13.3 新建切片并生效(高代价:2~6 分钟,断全部会话)

```bash
curl -s -X POST http://127.0.0.1:8099/api/slices/activate -H 'Content-Type: application/json' \
  -d '{"sst":3,"sd":"000003","imsi":"208950000000099"}'
```

**预期日志**:
```
核心切片清单:新增 3/000003 到 AMF/SMF 配置并重启(断注册)
✓ UE 请求的 NSSAI = 3.000003  实际 3.000003
✓ RAN 三份配置都广告了新切片
✓ AMF/SMF 配置含切片 3/000003
✓ UPF 注册 S-NSSAI = 3/000003
✓ 订户签约 singleNssai = 3.000003
✓ UE 已重建会话  9s, IP 12.1.1.3
✓ 新切片上能访问 ext-dn
```
**说明**:7 项全过 = 新切片在"核心 + RAN + UPF + 订户 + UE"五个面都落了地,而且 UE 真能上网。
**失败**:`UPF could not be selected` → 核心配置里没这个 S-NSSAI(看 13.4)。

### 13.4 核对核心配置

```bash
grep -n "slice_3_000003" -A3 /home/ubuntu/nwdaf-rel17/oai-cn5g-fed/docker-compose/conf/basic_vpp_nrf_config.yaml
```

**预期**:一处 anchor + AMF nssai 列表 + SMF 的 `sNssaiSmfInfoList` 和 `local_subscription_infos`。
**说明**:这四处缺一不可;文件是宿主挂载的,改完必须重启 AMF/SMF 才生效。

### 13.5 切换活动 UE

```bash
curl -s -X POST http://127.0.0.1:8099/api/ues/activate -H 'Content-Type: application/json' \
  -d '{"imsi":"208950000000099"}'
```

**预期**:`✓ ue.conf 的 IMSI = ...`、`✓ UE 请求切片 = 3.000003`、`✓ 已重建会话 9s`、`✓ 用户面可达`。
**说明**:本环境只能有一个 UE(`oaitun_ue1` 固定),"新增 UE" 后要"激活"才真的在跑。
