"""
增量式任务管理工具 + JSON 持久化 + 依赖关系。

替代原 TodoWriteTool（全量替换模式），改为 4 个独立工具：
- task_create: 创建单条任务
- task_update: 更新单条任务状态/内容
- task_get:    查看单条任务详情
- task_list:   列出所有任务

持久化：.runtime/state/tasks.json（跨会话保留）
依赖关系：blocked_by / blocks 字段
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from whalefall.core.log import get_logger
from whalefall.tools.base import BuiltinTool, ToolContext

_logger = get_logger("whalefall.tools.todo")

TaskStatus = Literal["pending", "in_progress", "completed"]
TaskPriority = Literal["high", "medium", "low"]

VALID_STATUSES = {"pending", "in_progress", "completed"}
VALID_PRIORITIES = {"high", "medium", "low"}
MAX_TASKS = 200

STATUS_ICON = {"pending": "○", "in_progress": "◑", "completed": "●"}
PRIORITY_ICON = {"high": "↑", "medium": "→", "low": "↓"}


# ── Task 数据类 ──────────────────────────────────────────────────────────


@dataclass
class Task:
    id: str
    content: str
    status: str = "pending"
    priority: str = "medium"
    blocked_by: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Task":
        return Task(
            id=d["id"],
            content=d["content"],
            status=d.get("status", "pending"),
            priority=d.get("priority", "medium"),
            blocked_by=d.get("blocked_by", []),
        )


# ── TaskStore：CRUD + 持久化 ─────────────────────────────────────────────


class TaskStore:
    """任务存储，支持增量 CRUD 和 JSON 文件持久化。"""

    def __init__(self, persist_path: Optional[Path] = None):
        self._tasks: Dict[str, Task] = {}
        self._counter = 0
        self._persist_path = persist_path
        self._load()

    def _next_id(self) -> str:
        self._counter += 1
        return f"task-{self._counter}"

    # ── CRUD ──────────────────────────────────────────────────────────

    def create(
        self,
        content: str,
        priority: str = "medium",
        blocked_by: Optional[List[str]] = None,
    ) -> Task:
        if blocked_by:
            for bid in blocked_by:
                if bid not in self._tasks:
                    raise ValueError(f"blocked_by 引用了不存在的任务: {bid}")
        task = Task(
            id=self._next_id(),
            content=content,
            status="pending",
            priority=priority if priority in VALID_PRIORITIES else "medium",
            blocked_by=blocked_by or [],
        )
        self._tasks[task.id] = task
        self._save()
        return task

    def update(
        self,
        task_id: str,
        status: Optional[str] = None,
        content: Optional[str] = None,
        priority: Optional[str] = None,
        blocked_by: Optional[List[str]] = None,
    ) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")
        if status is not None:
            if status not in VALID_STATUSES:
                raise ValueError(f"无效状态: {status}，应为 {VALID_STATUSES}")
            task.status = status
        if content is not None:
            task.content = content
        if priority is not None and priority in VALID_PRIORITIES:
            task.priority = priority
        if blocked_by is not None:
            for bid in blocked_by:
                if bid not in self._tasks:
                    raise ValueError(f"blocked_by 引用了不存在的任务: {bid}")
                if bid == task_id:
                    raise ValueError("任务不能阻塞自身")
            task.blocked_by = blocked_by
        self._save()
        return task

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_all(self) -> List[Task]:
        return list(self._tasks.values())

    def active_tasks(self) -> List[Task]:
        return [t for t in self._tasks.values() if t.status in ("pending", "in_progress")]

    # ── 依赖查询 ──────────────────────────────────────────────────────

    def blocked_tasks(self, task_id: str) -> List[str]:
        """返回被 task_id 阻塞的任务 ID 列表（即：谁的 blocked_by 含 task_id）。"""
        return [t.id for t in self._tasks.values() if task_id in t.blocked_by]

    def is_blocked(self, task_id: str) -> bool:
        """检查任务是否被未完成的前置任务阻塞。"""
        task = self._tasks.get(task_id)
        if not task or not task.blocked_by:
            return False
        return any(
            self._tasks.get(bid) and self._tasks[bid].status != "completed"
            for bid in task.blocked_by
        )

    # ── 持久化 ────────────────────────────────────────────────────────

    def _save(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "counter": self._counter,
                "tasks": [t.to_dict() for t in self._tasks.values()],
            }
            self._persist_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            _logger.warning(
                "task store save failed | path=%s err=%s",
                str(self._persist_path), exc,
            )

    def _load(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            self._counter = data.get("counter", 0)
            for td in data.get("tasks", []):
                task = Task.from_dict(td)
                self._tasks[task.id] = task
        except Exception as exc:
            _logger.warning(
                "task store load failed | path=%s err=%s",
                str(self._persist_path), exc,
            )


# ── 获取 / 渲染辅助 ──────────────────────────────────────────────────────


def get_task_store(ctx: ToolContext) -> TaskStore:
    """获取或创建 ToolContext 上的 TaskStore 实例（懒加载，自动持久化）。"""
    store = ctx.metadata.get("_task_store")
    if store is not None:
        return store
    from whalefall.core.runtime import state_dir
    persist_path = state_dir() / "tasks.json"
    store = TaskStore(persist_path=persist_path)
    ctx.metadata["_task_store"] = store
    return store


def render_task(task: Task, store: TaskStore) -> str:
    """渲染单条任务为一行文本。"""
    si = STATUS_ICON.get(task.status, "?")
    pi = PRIORITY_ICON.get(task.priority, "→")
    dep_str = ""
    if task.blocked_by:
        unfinished = [
            bid for bid in task.blocked_by
            if store.get(bid) and store.get(bid).status != "completed"  # type: ignore[union-attr]
        ]
        if unfinished:
            dep_str = f" ⛔ blocked by: {', '.join(unfinished)}"
        else:
            dep_str = " ✓ deps done"
    return f"  {si} [{pi}] {task.id}: {task.content}{dep_str}"


def render_task_list(tasks: List[Task], store: TaskStore) -> str:
    if not tasks:
        return "（无任务）"
    return "\n".join(render_task(t, store) for t in tasks)


def render_summary(store: TaskStore) -> str:
    tasks = store.list_all()
    if not tasks:
        return "任务列表为空"
    completed = sum(1 for t in tasks if t.status == "completed")
    in_progress = sum(1 for t in tasks if t.status == "in_progress")
    pending = sum(1 for t in tasks if t.status == "pending")
    return f"● 完成 {completed}  ◑ 进行中 {in_progress}  ○ 待处理 {pending}  共 {len(tasks)} 项"


# ── 工具类 ───────────────────────────────────────────────────────────────


class TaskCreateTool(BuiltinTool):
    """创建一条新任务，返回任务 ID。"""

    name = "task_create"
    description = (
        "创建一条新任务。返回任务 ID。\n"
        "可设置优先级（high/medium/low）和依赖关系（blocked_by: 被哪些任务阻塞）。"
    )
    read_only = False
    max_result_chars = 4_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "任务内容描述"},
            "priority": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "优先级（默认 medium）",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "阻塞此任务的前置任务 ID 列表（可选）",
            },
        },
        "required": ["content"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        content = (args.get("content") or "").strip()
        if not content:
            return "错误：content 不能为空"
        store = get_task_store(ctx)
        if len(store.list_all()) >= MAX_TASKS:
            return f"错误：任务数已达上限 {MAX_TASKS}"
        priority = args.get("priority", "medium")
        blocked_by = args.get("blocked_by") or []
        try:
            task = store.create(content, priority, blocked_by)
        except ValueError as e:
            return f"错误：{e}"
        return f"已创建任务 {task.id}\n{render_task(task, store)}\n\n{render_summary(store)}"

    def prompt(self) -> str:
        return (
            "任务管理（task_create / task_update / task_get / task_list）：\n"
            "- 接到复杂多步骤任务时，先用 task_create 逐条创建子步骤。\n"
            "- 开始执行某步骤时用 task_update 将其 status 改为 in_progress；完成后改为 completed。\n"
            "- 每次只操作单条任务（增量操作），不需要重写整个列表。\n"
            "- 支持 blocked_by 依赖关系：被阻塞的任务只有依赖完成后才应执行。\n"
            "- 任务跨会话持久化，下次对话打开时仍在。\n"
            "- 任务全部完成后输出最终总结。"
        )


class TaskUpdateTool(BuiltinTool):
    """更新一条任务的状态或内容。"""

    name = "task_update"
    description = (
        "更新一条任务的状态或内容。\n"
        "传入 task_id + 要更新的字段（status/content/priority/blocked_by）。"
    )
    read_only = False
    max_result_chars = 4_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID（如 task-1）"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed"],
                "description": "新状态",
            },
            "content": {"type": "string", "description": "新内容（可选）"},
            "priority": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "新优先级（可选）",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "新的阻塞任务 ID 列表（可选，覆盖原有）",
            },
        },
        "required": ["task_id"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return "错误：task_id 不能为空"
        store = get_task_store(ctx)
        try:
            task = store.update(
                task_id,
                status=args.get("status"),
                content=args.get("content"),
                priority=args.get("priority"),
                blocked_by=args.get("blocked_by"),
            )
        except (KeyError, ValueError) as e:
            return f"错误：{e}"
        return f"已更新任务 {task.id}\n{render_task(task, store)}\n\n{render_summary(store)}"


class TaskGetTool(BuiltinTool):
    """查看一条任务的详情。"""

    name = "task_get"
    description = "查看一条任务的详情（状态、优先级、依赖关系）。"
    read_only = True
    max_result_chars = 2_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID（如 task-1）"},
        },
        "required": ["task_id"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return "错误：task_id 不能为空"
        store = get_task_store(ctx)
        task = store.get(task_id)
        if task is None:
            available = [t.id for t in store.list_all()][:20]
            return f"错误：任务 {task_id} 不存在。现有任务：{', '.join(available) or '无'}"
        blocks = store.blocked_tasks(task_id)
        lines = [render_task(task, store)]
        if blocks:
            lines.append(f"  → 阻塞了: {', '.join(blocks)}")
        return "\n".join(lines)


class TaskListTool(BuiltinTool):
    """列出所有任务（或按状态过滤）。"""

    name = "task_list"
    description = "列出所有任务（或按状态过滤），查看当前进度。"
    read_only = True
    max_result_chars = 8_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "all"],
                "description": "按状态过滤（默认 all）",
            },
        },
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        store = get_task_store(ctx)
        status_filter = (args.get("status") or "all").strip().lower()
        tasks = store.list_all()
        if status_filter != "all":
            tasks = [t for t in tasks if t.status == status_filter]
        return f"{render_summary(store)}\n\n{render_task_list(tasks, store)}"
