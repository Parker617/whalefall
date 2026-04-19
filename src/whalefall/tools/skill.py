"""
SkillTool：内建 skill 加载工具。

职责：
- 扫描多级 skills 目录，提取 name + description 摘要
- 按 skill 名加载全文（工具调用）
- 记录 invoked_skills（供 context 压缩后恢复）
- 按 agent 的 allowed_skill_paths 过滤可见目录
  （None = 全看；[prefix, ...] = 仅前缀匹配；[] = 全不看）

Skill 目录：whalefall/skills/（所有 skill 统一存放于此）
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from whalefall.tools.base import BuiltinTool, ToolContext
from whalefall.core.log import truncate

MAX_LISTING_DESC_CHARS = 260
SKILL_LIST_BUDGET_CHARS = 7_000
MAX_INVOKED_SKILLS = 10
MAX_INVOKED_SKILL_CHARS = 25_000


class SkillTool(BuiltinTool):
    """按名称加载本地 SKILL.md。"""

    name = "skill"
    description = (
        "按名称加载本地技能文档（SKILL.md）全文。"
        "当任务与某个技能匹配时，先调用本工具加载对应 skill。"
    )
    read_only = True
    max_result_chars = 100_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "技能名称，例如 weather / fundamentals",
            },
            "args": {
                "type": "string",
                "description": "可选参数字符串（仅透传展示，不做解析）",
            },
        },
        "required": ["skill"],
    }

    @staticmethod
    def _skills_root() -> Path:
        """skill 目录：whalefall/skills/"""
        return Path(__file__).resolve().parents[1] / "skills"

    # ── frontmatter 解析 ──────────────────────────────────────────────

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[str, str]:
        text = content or ""
        m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
        if not m:
            return "", text
        return m.group(1), m.group(2)

    @staticmethod
    def _extract_frontmatter_value(frontmatter: str, key: str) -> str:
        """提取 frontmatter 的 key: value（单行）。"""
        if not frontmatter:
            return ""
        target = key.strip().lower()
        for raw in frontmatter.splitlines():
            line = raw.strip()
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            if k.strip().lower() == target:
                return v.strip().strip('"\'')
        return ""

    @staticmethod
    def _extract_first_paragraph(body: str) -> str:
        for para in (body or "").split("\n\n"):
            p = para.strip()
            if not p or p.startswith("#"):
                continue
            return p.replace("\n", " ").strip()
        return ""

    # ── skill 扫描与查找 ─────────────────────────────────────────────

    @staticmethod
    def _is_skill_allowed(
        rel_name: str,
        allowed_paths: Optional[List[str]],
    ) -> bool:
        """
        按 allowed_paths 前缀匹配 rel_name（形如 "finance/stock/factor_mining"）。

        - allowed_paths is None  → 全看（默认）
        - allowed_paths == []    → 全不看
        - 前缀以 "/" 结尾         → 目录前缀匹配（startswith），含嵌套
        - 前缀不以 "/" 结尾       → 精确 skill 名匹配
        """
        if allowed_paths is None:
            return True
        if not allowed_paths:
            return False
        for prefix in allowed_paths:
            p = (prefix or "").strip()
            if not p:
                continue
            if p.endswith("/"):
                # 目录前缀：rel_name 以 p 开头（rel_name 本身不带尾 /）
                if rel_name == p.rstrip("/") or rel_name.startswith(p):
                    return True
            else:
                if rel_name == p:
                    return True
        return False

    @classmethod
    def _scan_skills(
        cls,
        allowed_paths: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        """
        扫描 whalefall/skills/ 目录，提取 name + description + path。

        allowed_paths: 若传入，按前缀过滤；None 表示全看。
        """
        root = cls._skills_root()
        out: List[Dict[str, str]] = []
        if not root.exists():
            return out

        for md in sorted(root.rglob("SKILL.md")):
            try:
                rel_name = md.parent.relative_to(root).as_posix()
            except Exception:
                continue
            if not rel_name or rel_name == ".":
                continue
            if not cls._is_skill_allowed(rel_name, allowed_paths):
                continue

            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            front, body = cls._split_frontmatter(text)
            # skill 的调用键统一为 rel_name（相对 skills/ 的路径，如 "general/weather"）
            # frontmatter 里的 name 字段仅作信息展示，不再参与查找，避免"name 与目录不符"导致误导
            description = cls._extract_frontmatter_value(front, "description")
            if not description:
                description = cls._extract_first_paragraph(body) or "(no description)"

            out.append({
                "name": rel_name,
                "description": description.strip(),
                "path": str(md),
            })
        return out

    @classmethod
    def _find_skill_file(
        cls,
        skill_name: str,
        allowed_paths: Optional[List[str]] = None,
    ) -> Path | None:
        """
        按名称查找 skill 文件。防止路径穿越；若 allowed_paths 非 None，
        命中后还需通过前缀校验。
        """
        parts: List[str] = [p for p in skill_name.strip().split("/") if p]
        if not parts or any(p in {".", ".."} for p in parts):
            return None

        root = cls._skills_root().resolve()
        path = (root.joinpath(*parts) / "SKILL.md").resolve()
        try:
            rel = path.parent.relative_to(root).as_posix()
        except ValueError:
            return None
        if not path.is_file():
            return None
        if not cls._is_skill_allowed(rel, allowed_paths):
            return None
        return path

    # ── catalog / description ────────────────────────────────────────

    @classmethod
    def catalog_lines(
        cls,
        char_budget: int = SKILL_LIST_BUDGET_CHARS,
        allowed_paths: Optional[List[str]] = None,
    ) -> List[str]:
        """返回用于系统提醒的技能目录行：- name: description。"""
        skills = cls._scan_skills(allowed_paths=allowed_paths)
        lines: List[str] = []
        used = 0
        omitted = 0

        for s in skills:
            line = f"- {s['name']}: {truncate(s['description'], MAX_LISTING_DESC_CHARS)}"
            if used + len(line) > char_budget:
                omitted += 1
                continue
            lines.append(line)
            used += len(line) + 1

        if omitted > 0:
            lines.append(f"- ... 还有 {omitted} 个 skill 因提示词预算未展示")
        return lines

    @classmethod
    def _dynamic_tool_description(
        cls,
        allowed_paths: Optional[List[str]] = None,
    ) -> str:
        """构建 tool description：附带 available skills 摘要。"""
        lines = cls.catalog_lines(
            char_budget=SKILL_LIST_BUDGET_CHARS,
            allowed_paths=allowed_paths,
        )
        if not lines:
            return (
                "按名称加载本地技能文档（SKILL.md）全文。"
                "当前未发现可用技能（skills/ 目录为空、缺失，或当前 Agent 无权访问）。"
            )
        return (
            "按名称加载本地技能文档（SKILL.md）全文。"
            "当任务匹配某个技能时，先加载该技能再执行。"
            "可用技能：\n" + "\n".join(lines)
        )

    # ── 执行 ─────────────────────────────────────────────────────────

    @staticmethod
    def _record_invoked_skill(ctx: ToolContext, skill_name: str, skill_path: Path, content: str) -> None:
        """记录已调用 skill 到 ctx.invoked_skills，供 compaction 后恢复。"""
        entry = {
            "name": skill_name,
            "path": str(skill_path),
            "content": (content or "")[:MAX_INVOKED_SKILL_CHARS],
        }
        invoked = [x for x in ctx.invoked_skills if str(x.get("path", "")) != str(skill_path)]
        invoked.append(entry)
        ctx.invoked_skills = invoked[-MAX_INVOKED_SKILLS:]

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        skill_name = str(args.get("skill", "")).strip()
        skill_args = str(args.get("args", "")).strip()

        if not skill_name:
            return "错误：skill 参数不能为空"

        allowed_paths = ctx.allowed_skill_paths
        skill_file = self._find_skill_file(skill_name, allowed_paths=allowed_paths)

        if skill_file is None:
            # 列出当前 agent 可见的 skill 帮助 LLM 纠错（不泄露黑名单外的 skill 名）
            visible = [s["name"] for s in self._scan_skills(allowed_paths=allowed_paths)]
            preview = ", ".join(visible[:30]) + (" ..." if len(visible) > 30 else "")
            return (
                f"错误：skill '{skill_name}' 不存在或当前 Agent 无权访问。\n"
                f"可用技能: {preview if preview else '(none)'}"
            )

        try:
            content = skill_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"错误：读取 skill 失败: {exc}"

        self._record_invoked_skill(ctx, skill_name, skill_file, content)

        base_dir = str(skill_file.parent)
        arg_line = f"\n调用参数: {skill_args}" if skill_args else ""
        return (
            f"[SKILL LOADED]\n"
            f"名称: {skill_name}\n"
            f"目录: {base_dir}{arg_line}\n\n"
            f"{content}"
        )

    def to_openai_schema(self, agent_config: Any = None) -> Dict[str, Any]:
        """动态 schema：按 agent_config.allowed_skill_paths 过滤可见 skill 列表。"""
        allowed_paths: Optional[List[str]] = None
        if agent_config is not None:
            allowed_paths = getattr(agent_config, "allowed_skill_paths", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self._dynamic_tool_description(allowed_paths=allowed_paths),
                "parameters": self.parameters_schema,
            },
        }

    def prompt(self) -> str:
        return (
            "当任务匹配某个 skill 时，先调用 `skill` 工具加载全文，"
            "再按技能步骤执行。"
        )
