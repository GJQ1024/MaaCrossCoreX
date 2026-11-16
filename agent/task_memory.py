# -*- coding: utf-8 -*-
"""任务记忆 + 冷却调度：gate / complete 两个自定义动作与状态存储。

设计见 docs/task-memory.md。核心三字段：上次完成时间 + 冷却时长 + 今日已执行次数。
冷却与每日上限是两个独立条件，都满足才放行，跳过原因可区分。
本地记录只是提示，画面识别才是真值（reconcile 由 pipeline 侧节点接入）。

配置来源（优先级从低到高）：
1. 代码内置默认值（DEFAULT_POLICY）；
2. pipeline 节点 custom_action_param 的字段（任务定义自带，如 daily_limit）；
3. 用户策略文件 config/task_memory/<实例>.policy.json（用户手改，最终生效）。

强制开关：param "force": true 或环境变量 TASK_MEMORY_FORCE=1，只无视冷却，上限仍拦。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from maa.custom_action import CustomAction
from maa.context import Context

log = logging.getLogger("laa.task_memory")

SCHEMA_VERSION = 1
REASON_COOLDOWN = "cooldown"
REASON_LIMIT = "limit"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 内置默认策略：无冷却、不限次数、每日 05:00 刷新（05:00 为待核实默认值，可配置）
DEFAULT_POLICY = {
    "cooldown_minutes": 0,
    "daily_limit": 0,
    "refresh_hour": 5,
    "refresh_weekday": None,
}

# 时钟回拨容忍：完成时间比当前时间晚超过该值才判定为回拨（秒）
CLOCK_SKEW_TOLERANCE = 300


def game_day_id(now: datetime, refresh_hour: int) -> str:
    """游戏日：刷新时刻到次日刷新时刻算同一天，id 取区间起始日期。"""
    anchor = now.replace(hour=refresh_hour, minute=0, second=0, microsecond=0)
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor.date().isoformat()


def _game_week_id(now: datetime, refresh_weekday: int, refresh_hour: int) -> str:
    anchor = now.replace(hour=refresh_hour, minute=0, second=0, microsecond=0)
    anchor -= timedelta(days=(now.weekday() - refresh_weekday) % 7)
    if now < anchor:
        anchor -= timedelta(days=7)
    return f"W{anchor.date().isoformat()}"


@dataclass
class TaskPolicy:
    cooldown_minutes: int = 0
    daily_limit: int = 0
    refresh_hour: int = 5
    refresh_weekday: int | None = None

    @classmethod
    def from_sources(cls, param: dict, policy_override: dict) -> "TaskPolicy":
        merged = dict(DEFAULT_POLICY)
        for src in (param, policy_override):
            for key in DEFAULT_POLICY:
                if key in src and src[key] is not None:
                    merged[key] = src[key]
        return cls(
            cooldown_minutes=int(merged["cooldown_minutes"]),
            daily_limit=int(merged["daily_limit"]),
            refresh_hour=int(merged["refresh_hour"]),
            refresh_weekday=(
                None if merged["refresh_weekday"] is None
                else int(merged["refresh_weekday"])
            ),
        )


@dataclass
class GateDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    count: int = 0
    limit: int = 0
    next_available_at: str | None = None

    def reason_text(self) -> str:
        parts = []
        if REASON_COOLDOWN in self.reasons:
            parts.append("冷却未到期")
        if REASON_LIMIT in self.reasons:
            parts.append("今日次数已用完")
        return "、".join(parts) if parts else "允许执行"


def memory_dir() -> Path:
    """状态目录：默认 <克隆根>/config/task_memory，可用 TASK_MEMORY_DIR 覆盖（测试用）。"""
    override = os.environ.get("TASK_MEMORY_DIR")
    return Path(override) if override else PROJECT_ROOT / "config" / "task_memory"


def _account_id() -> str:
    """与 chip_filter_flow.instance_config_path 同款实例来源；缺省 default。"""
    configured = os.environ.get("MAA_INSTANCE_CONFIG")
    if configured:
        return Path(configured).stem
    instance_id = os.environ.get("MFA_INSTANCE_ID", "").strip()
    return instance_id or "default"


def store_path(account: str | None = None) -> Path:
    return memory_dir() / f"{account or _account_id()}.json"


def policy_path(account: str | None = None) -> Path:
    return memory_dir() / f"{account or _account_id()}.policy.json"


def skip_event_path(account: str | None = None) -> Path:
    """跳过事件流：与完成记录分开存放，串跑复盘可区分「跳过」与「完成」。"""
    return memory_dir() / f"{account or _account_id()}.events.jsonl"


def record_gate_skip(task: str, decision: GateDecision) -> None:
    """门禁拦截时追加一条 event=skip；写入失败不影响主流程。"""
    try:
        path = skip_event_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": "skip",
            "task": task,
            "reasons": list(decision.reasons),
            "count": decision.count,
            "limit": decision.limit,
            "next_available_at": decision.next_available_at,
        }
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("跳过事件写入失败（不影响主流程）：%s", exc)


def load_policy_override(account: str | None = None) -> dict:
    """用户策略文件：{任务名: {policy 字段}} 或 {policy 字段}（全局）。"""
    path = policy_path(account)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("策略文件损坏，按无覆盖处理：%s (%s)", path, exc)
        return {}


class TaskMemoryStore:
    """JSON 状态存储：原子写入、损坏备份重建、时钟回拨防护。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                # 损坏：备份后重建，不静默覆盖，不让任务永久不可用
                backup = self.path.with_suffix(self.path.suffix + ".corrupt")
                try:
                    os.replace(self.path, backup)
                    log.warning("状态文件损坏，已备份到 %s 并重建：%s", backup, exc)
                except OSError:
                    log.error("状态文件损坏且备份失败，按空记录重建：%s", exc)
        return {"version": SCHEMA_VERSION, "account": self.path.stem, "tasks": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _get(self, task: str) -> dict:
        return self._data["tasks"].setdefault(task, {})

    def _rollover(self, st: dict, policy: TaskPolicy, now: datetime) -> None:
        if policy.refresh_weekday is not None:
            cur = _game_week_id(now, policy.refresh_weekday, policy.refresh_hour)
        else:
            cur = game_day_id(now, policy.refresh_hour)
        if st.get("day_id") != cur:
            st["day_id"] = cur
            st["today_count"] = 0

    @staticmethod
    def _clock_rolled_back(st: dict, now: datetime) -> bool:
        """时钟回拨：记录的上次完成时间明显在未来。"""
        last = st.get("last_completed_at")
        if not last:
            return False
        try:
            return datetime.fromisoformat(last) > now + timedelta(seconds=CLOCK_SKEW_TOLERANCE)
        except ValueError:
            return False

    def check(self, task: str, policy: TaskPolicy, now: datetime | None = None,
              force: bool = False) -> GateDecision:
        """门禁判定。force=True 只无视冷却，每日上限仍然拦截。"""
        now = now or datetime.now()
        with self._lock:
            st = self._get(task)
            clock_back = self._clock_rolled_back(st, now)
            if clock_back:
                # 时钟回拨：不凭一条"未来的"记录拦截用户，冷却视为已到期、
                # 计数交给画面真值兜底（任务内识别会纠正），并记日志提示。
                log.warning(
                    "检测到时钟回拨：任务 %s 的完成时间 %s 晚于当前时间 %s，"
                    "本次不按该记录拦截",
                    task, st.get("last_completed_at"), now.isoformat(timespec="seconds"),
                )
                st["last_completed_at"] = None
            self._rollover(st, policy, now)
            self._save()

            reasons: list[str] = []
            next_at: str | None = None

            if policy.cooldown_minutes > 0 and st.get("last_completed_at"):
                last = datetime.fromisoformat(st["last_completed_at"])
                ready = last + timedelta(minutes=policy.cooldown_minutes)
                if now < ready:
                    if not force:
                        reasons.append(REASON_COOLDOWN)
                    next_at = ready.isoformat(timespec="seconds")

            count = st.get("today_count", 0)
            if policy.daily_limit > 0 and count >= policy.daily_limit:
                reasons.append(REASON_LIMIT)  # force 不豁免上限
                if policy.refresh_weekday is None:
                    anchor = now.replace(hour=policy.refresh_hour, minute=0,
                                         second=0, microsecond=0)
                    if now >= anchor:
                        anchor += timedelta(days=1)
                    next_at = anchor.isoformat(timespec="seconds")

            return GateDecision(
                allowed=not reasons,
                reasons=reasons,
                count=count,
                limit=policy.daily_limit,
                next_available_at=next_at,
            )

    def mark_completed(self, task: str, policy: TaskPolicy,
                       now: datetime | None = None) -> dict:
        """仅任务真实成功后由 complete 动作调用。中断/失败禁止调用。"""
        now = now or datetime.now()
        with self._lock:
            st = self._get(task)
            self._rollover(st, policy, now)
            st["today_count"] = st.get("today_count", 0) + 1
            st["last_completed_at"] = now.isoformat(timespec="seconds")
            st["last_result"] = "succeeded"
            st["updated_at"] = st["last_completed_at"]
            self._save()
            log.info("任务 %s 记为已完成：今日 %d 次", task, st["today_count"])
            return dict(st)

    def reconcile(self, task: str, policy: TaskPolicy, screen_already_done: bool,
                  now: datetime | None = None) -> dict:
        """画面真值纠正：记录与画面冲突时以画面为准回写。"""
        now = now or datetime.now()
        with self._lock:
            st = self._get(task)
            self._rollover(st, policy, now)
            if screen_already_done and st.get("today_count", 0) == 0:
                st["today_count"] = 1
                st["last_completed_at"] = now.isoformat(timespec="seconds")
                st["last_result"] = "reconciled_done"
            elif (not screen_already_done and st.get("today_count", 0) > 0
                  and st.get("last_result") == "succeeded"):
                st["today_count"] = 0
                st["last_completed_at"] = None
                st["last_result"] = "reconciled_undone"
            st["updated_at"] = now.isoformat(timespec="seconds")
            self._save()
            return dict(st)

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))


def _parse_param(argv) -> tuple[str, TaskPolicy, bool]:
    """解析 custom_action_param（注意 override 会双重编码）。返回 (任务名, 策略, 强制)。"""
    raw = getattr(argv, "custom_action_param", None) or "{}"
    param = json.loads(raw)
    if isinstance(param, str):
        param = json.loads(param)
    if not isinstance(param, dict):
        raise ValueError(f"custom_action_param 不是对象：{raw!r}")
    task = str(param.get("task") or "unknown")
    force = bool(param.get("force", False)) or os.environ.get("TASK_MEMORY_FORCE") == "1"
    account = param.get("account")
    override_all = load_policy_override(account)
    # 支持 {"任务名": {...}} 与平铺全局两种写法，任务级优先
    override = override_all.get(task, {}) if any(
        isinstance(v, dict) for v in override_all.values()) else override_all
    policy = TaskPolicy.from_sources(param, override)
    return task, policy, force


# 动作实例必须强引用：绑定层用对象 id 做回调 trans_arg，临时对象被 GC 后
# 回调会拿到复用内存里的杂对象。模块级持有。
_INSTANCES: list = []


class TaskMemoryGateAction(CustomAction):
    """链首门禁：返回 True 放行；False 使节点失败走 on_error 早退。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            task, policy, force = _parse_param(argv)
        except (json.JSONDecodeError, ValueError) as exc:
            # 参数错误宁可放行并告警，不让用户任务永远跑不了
            log.error("门禁参数解析失败，按放行处理：%s", exc)
            return True
        decision = TaskMemoryStore(store_path()).check(task, policy, force=force)
        if decision.allowed:
            log.info("任务记忆放行 %s：今日 %d/%s%s", task, decision.count,
                     decision.limit or "∞", "（强制：仅无视冷却）" if force else "")
            return True
        log.warning("任务记忆跳过 %s：%s（今日 %d/%s，下次可跑 %s）——本次为【跳过】，"
                    "未执行任何业务步骤，不等同于完成", task,
                    decision.reason_text(), decision.count, decision.limit or "∞",
                    decision.next_available_at)
        record_gate_skip(task, decision)
        return False


class TaskMemoryCompleteAction(CustomAction):
    """链尾完成节点：任务真实成功后写记录。中断/失败时该节点不执行，不留假记录。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            task, policy, _force = _parse_param(argv)
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("完成节点参数解析失败，不写记录：%s", exc)
            return False
        TaskMemoryStore(store_path()).mark_completed(task, policy)
        return True


_INSTANCES.extend([TaskMemoryGateAction(), TaskMemoryCompleteAction()])
