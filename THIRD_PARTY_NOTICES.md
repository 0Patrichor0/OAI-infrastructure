# 第三方来源与许可证

本仓库是部署集成、差异补丁、配置及定制控制代码的集合。它没有为所有内容授予一个统一的新许可证；请依据文件现有头部和所属上游许可证使用、修改与分发。

| 上游 | 固定版本记录 | 随附许可证 |
| --- | --- | --- |
| EURECOM netsoft/nwdaf-rel17 | `upstreams.lock.json` 的 nwdaf 项 | `licenses/nwdaf-LICENSE` |
| OAI oai-cn5g-nwdaf / AnLF | anlf 项 | `licenses/anlf-LICENSE` |
| OAI oai-cn5g-fed | core 项 | `licenses/core-LICENSE` |
| duranta-project/openairinterface5g | oai 项 | `licenses/oai-LICENSE` |
| duranta-project/flexric | flexric 项 | `licenses/flexric-LICENSE` |
| mouse07410/asn1c | asn1c 项 | 获取源码后查阅其 COPYING、LICENSE 和文件头 |

`patches/` 修改相应上游代码，`overlays/` 中部分文件也源于上游。保留的许可证和头部不应移除。容器镜像、镜像内软件、系统依赖与模型分别遵循自己的许可证，不因本仓库记录镜像名称或构建步骤而重新授权。

本仓库没有上传上游完整源码或容器镜像。获取脚本从 `upstreams.lock.json` 所列地址下载固定版本，具体许可证应同时以该固定版本中的原文为准。
