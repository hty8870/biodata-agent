# -*- coding: utf-8 -*-
"""锚定的 secret 值模式 + 扫描/脱敏助手（单一真源，交付复核、质量门 report 脱敏与
MCP 调用留痕值级脱敏三方共用）。

2026-08-10 P0-2（codex 二轮评审）：自 `scripts/secret_patterns.py` 提升为 src 公共模块——
mcp_server 等 src 侧消费方不该反向依赖 scripts/（分层方向：scripts → src，永不反转）。
`scripts/secret_patterns.py` 保留兼容壳重导出，既有引用点零改动。

设计约束（三方对抗评审共识）：
1. **只用强锚定的服务商/云凭据前缀，绝不用裸熵 / 通用 hex 正则。** 本仓库交付集本身就是
   md5 / SHA-256 / 大小 元数据目录（release-manifest 满是 hex 摘要）；任何"高熵串/像密钥的 hex"
   正则会在交付物自己的核心数据上**持续误报**，几次假阳后这道门就没人信了。
2. **`sk-` 的 body 只含 alnum（不含连字符）**，因为认领/交接命名规约是 `<tool>-<task-slug>-<id>`，
   "ta**sk-**slug-…" 含 `sk-`；alnum-only body 让 "slug" 后的连字符断开匹配，避开这个高频误报。
   这些前缀（sk-/ghp_/AKIA/AIza/xox*-/eyJ）都不会出现在 md5/sha256/task-slug 里，近零误报。
3. **只报 pattern_id + file:line，绝不回显命中的实际子串**——否则把真 secret 写进 CI/stderr 日志
   就是二次泄漏。redact() 也只把命中值替换成 [REDACTED:<id>]，不保留原值。

这些模式定义字符串本身**不自匹配**（前缀后紧跟 `[`，不是 alnum/base64），故本文件被交付扫描时不会自报。
"""
from __future__ import annotations

import re

# (pattern_id, compiled_regex)
SECRET_VALUE_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # 现代带连字符前缀的 LLM key（Anthropic sk-ant-…、OpenAI sk-proj-/sk-svcacct-/sk-admin-…）：
    # 前缀 sk-ant-/sk-proj-/… 足够独特（绝不出现在 task-slug 里），故 body 允许连字符/下划线（base64url）。
    # 本项目以 Claude/LLM 为核心，这些恰是最可能被误粘的 key，必须单列——legacy 纯 alnum 模式抓不到它们。
    ("scoped-llm-key", re.compile(r"sk-(?:ant|proj|svcacct|admin)-[A-Za-z0-9_-]{20,}")),
    # legacy 纯 alnum OpenAI key（body 只含 alnum，避免 ta+sk-slug 命名规约误报）
    ("openai-secret-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"gh[oprsu]_[A-Za-z0-9]{36}")),
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("slack-token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}")),
]


def find_secret_patterns(text: str) -> set[str]:
    """返回文本命中的 pattern_id 集合（**不含**命中的实际值）。"""
    hits: set[str] = set()
    for pattern_id, rx in SECRET_VALUE_PATTERNS:
        if rx.search(text):
            hits.add(pattern_id)
    return hits


def redact(text: str | None) -> str | None:
    """把命中的 secret 值替换成 [REDACTED:<pattern_id>]；None/空原样返回。"""
    if not text:
        return text
    for pattern_id, rx in SECRET_VALUE_PATTERNS:
        text = rx.sub(f"[REDACTED:{pattern_id}]", text)
    return text
