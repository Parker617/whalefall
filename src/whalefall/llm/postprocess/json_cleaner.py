"""
从 LLM 输出中抽取合法 JSON 的清洗工具。

- `clean_json`：总入口，返回"看起来最像 JSON 的一段字符串"
- `fix_unescaped_quotes`：字符串字面量里未转义的 `"` 修补
- `extract_json_like`：粗暴定位最外层 `{...}` / `[...]`
"""
from __future__ import annotations

import json
import re
from typing import List


def clean_json(response: str) -> str:
    """
    从 LLM 输出里抽出合法 JSON 段：
      1. 按 ```code fence``` 切片；
      2. 对每个 candidate 依次尝试 `extract_json_like` + `fix_unescaped_quotes`；
      3. 任何一个 `json.loads` 成功即返回；
      4. 若都失败，返回最长的候选（让上层再决定要不要 raise）。
    """
    text = (response or "").strip()
    if not text:
        return text

    candidates: List[str] = [text]
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part:
                candidates.append(part)

    for cand in candidates:
        json_like = extract_json_like(cand)
        for attempt in (json_like, fix_unescaped_quotes(json_like)):
            try:
                json.loads(attempt)
                return attempt.strip()
            except Exception:
                pass

    fallback: List[str] = []
    for cand in candidates:
        if "{" in cand or "[" in cand:
            jl = extract_json_like(cand)
            fallback.extend([jl, fix_unescaped_quotes(jl)])
    for cand in fallback:
        try:
            json.loads(cand)
            return cand.strip()
        except Exception:
            pass
    if fallback:
        return max(fallback, key=len).strip()
    return text


def fix_unescaped_quotes(json_str: str) -> str:
    """
    把字符串字面量内部未转义的 `"` 改成 `\"`，同时保留结束分隔的 `"`。
    依据：若 `"` 后面紧跟 `:` / `}` / `]` / `,`，则视为分隔符；否则视为内部字符。
    """
    result: List[str] = []
    in_string = False
    escape_next = False
    for i, char in enumerate(json_str):
        if escape_next:
            result.append(char)
            escape_next = False
        elif char == "\\":
            result.append(char)
            escape_next = True
        elif char == '"':
            if in_string:
                j = i + 1
                while j < len(json_str) and json_str[j] in " \t\n\r":
                    j += 1
                if j >= len(json_str) or json_str[j] in ":},]":
                    result.append(char)
                    in_string = False
                else:
                    result.append('\\"')
            else:
                result.append(char)
                in_string = True
        else:
            result.append(char)
    return "".join(result)


def extract_json_like(s: str) -> str:
    """
    从文本里粗暴定位最外层 `{...}` 或 `[...]`：
      - 若首行是裸词（语言标记，如 `json`），跳过第一行；
      - 按 `{` / `[` 平衡匹配，返回到平衡点的子串；匹配不上则原样返回。
    """
    s = (s or "").strip()
    if not s:
        return s
    lines = s.splitlines()
    if len(lines) > 1 and re.fullmatch(r"[a-zA-Z0-9_+\-]+", lines[0].strip()):
        s = "\n".join(lines[1:]).strip()

    first_obj, first_arr = s.find("{"), s.find("[")
    if first_obj == -1 and first_arr == -1:
        return s
    if first_obj == -1:
        start_idx, open_char, close_char = first_arr, "[", "]"
    elif first_arr == -1 or first_obj < first_arr:
        start_idx, open_char, close_char = first_obj, "{", "}"
    else:
        start_idx, open_char, close_char = first_arr, "[", "]"

    depth, end_idx = 0, -1
    for i in range(start_idx, len(s)):
        if s[i] == open_char:
            depth += 1
        elif s[i] == close_char:
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    return s[start_idx : end_idx + 1].strip() if end_idx != -1 else s
