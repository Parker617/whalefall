"""
长文本清洗工具：去页眉页脚高频重复行 + 按关键字截断尾部附录/声明段。

常见用法是把券商研报、公告类的 PDF 抽取文本喂进来，得到更干净的主正文。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import List, Optional


def clean_main_text(
    raw: str,
    strong_keywords: Optional[List[str]] = None,
    end_section_keywords: Optional[List[str]] = None,
    weak_keywords: Optional[List[str]] = None,
    end_section_threshold: float = 0.7,
    weak_keywords_threshold: float = 0.8,
    header_footer_min_count: int = 10,
    header_footer_max_length: int = 80,
) -> str:
    """清洗主文本：去除高频页眉页脚、截断尾部声明/附录。"""
    strong_keywords = strong_keywords or []
    end_section_keywords = end_section_keywords or []
    weak_keywords = weak_keywords or []

    lines = (raw or "").splitlines()
    counter = Counter(line.strip() for line in lines if line.strip())
    min_count = max(3, header_footer_min_count)
    frequent = {
        text for text, count in counter.items()
        if count >= min_count and len(text) < header_footer_max_length
    }

    cleaned: List[str] = []
    skip_rest = False
    total_lines = len(lines)

    for idx, line in enumerate(lines):
        s = line.strip()
        pos = idx / total_lines if total_lines > 0 else 0.0

        if not s:
            cleaned.append("")
            continue
        if skip_rest:
            continue
        if s in frequent:
            continue

        lower = s.lower()
        has_end_kw = any(kw in lower for kw in end_section_keywords)
        has_strong_kw = any(kw in lower for kw in strong_keywords)
        has_weak_kw = any(kw in lower for kw in weak_keywords)
        is_strict_title = s.startswith("#") or s.isupper()

        if has_strong_kw and is_strict_title and pos >= 0.5:
            title_core = re.sub(r"^[#\s0-9.\-]+", "", s)
            title_core = re.sub(r"[:：\s]+$", "", title_core).strip().lower()
            if any(
                title_core == kw
                or title_core.startswith(kw + " ")
                or (kw in title_core and len(title_core) <= len(kw) + 20)
                for kw in strong_keywords
            ):
                skip_rest = True
                continue

        if has_end_kw and pos >= end_section_threshold and is_strict_title:
            skip_rest = True
            continue

        if has_weak_kw:
            max_length = 50 if "required disclosures" in lower else 40
            is_title_with_short = is_strict_title or (
                len(s) < 80 and (has_strong_kw or has_weak_kw)
            )
            if (
                is_title_with_short
                and len(s) < max_length
                and pos >= weak_keywords_threshold
            ):
                skip_rest = True
                continue

        cleaned.append(line)

    return "\n".join(cleaned).strip()
