"""意图驱动执行框架 —— 把自然语言意图落到 OAI 5GC / O-RAN 的真实配置面。

模块划分:
  db        —— MySQL(oai_db)读写、行级快照/恢复
  ue        —— 活动 UE 的会话重建与等待
  nl        —— 自然语言 → 结构化意图(LLM 优先,规则兜底)
  actions   —— 意图动作(新增用户 / 修改 QoS / ...)
  ledger    —— 意图账本(sqlite)+ 审计流水(JSONL,可回放)
"""
__version__ = "0.1.0"
