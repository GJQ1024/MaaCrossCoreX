# -*- coding: utf-8 -*-
"""任务级 NextRun 自报：任务在真正「排产/换班成功」的点写入下次不早于时间。

壳（MFA）在任务完成时读取并消费 config/job_next/<实例>.json：
- chip_build：只有「开始改造」成功才写，满仓/无片/槽位不足不经过该节点；
- infra_shift：换班动作结束时写，swapped=true 表示本次确有换班，false 表示全绿巡检。

芯片改造：节点会现场 OCR「改造中心」的 `所需时间 NN:NN:NN`，
取可见槽位最小值 + 120s 收尾缓冲写 notBefore；读不到/格式不符则不写 notBefore，
由壳按实测间隔（≥3 样本）或静态间隔退路，不猜默认 4 小时。
换班：写 swapped 标记与 480 分钟兜底，壳会按实测换班周期校正。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from maa.context import Context
from maa.custom_action import CustomAction

log = logging.getLogger("laa.job_next")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 「改造中心」面板「所需时间」数值列（1280×720 原生帧实测）。
# 三行文本的数值区 x=1021..1101，行 y 起点 226 / 373 / 520（行距 147，字高 14~15）。
# ROI 左右各留 8~9px、上下各留 14px，只框数值列，不含「所需时间」中文标签。
REMAINING_TIME_ROI = [1010, 212, 100, 340]
REMAINING_PATTERN = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")
# 收尾缓冲：调度器 30s 轮询 + 倒计时截断 <1s + 截图/OCR ~1s，
# 再加「收取」按钮可点前的刷新余量（改造节点自身识别超时 20s 内可自愈）。
COLLECT_BUFFER_SECONDS = 120.0


def parse_remaining_seconds(text: str) -> int | None:
    """`03:44:44` → 13484；格式不符返回 None（不猜默认值）。"""
    match = REMAINING_PATTERN.match((text or "").strip().replace(" ", ""))
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def read_panel_remaining(context: Context, image=None) -> int | None:
    """读「改造中心」当前可见页所有 `NN:NN:NN`，取最小值（秒）。

    读不到、格式不符、调用异常一律返回 None，由调度器走退路；
    不返回默认 4 小时，也不沿用上一次读数。
    """
    try:
        shot = image if image is not None else context.tasker.controller.post_screencap().wait().get()
        result = context.run_recognition(
            "通用_OCR探针",
            shot,
            pipeline_override={
                "通用_OCR探针": {
                    "recognition": "OCR",
                    "roi": REMAINING_TIME_ROI,
                    "expected": [],
                }
            },
        )
    except Exception as exc:  # noqa: BLE001 - 识别失败只降级，不影响任务链
        log.warning("剩余时间读数失败：%s", exc)
        return None

    if not result:
        return None

    seen: set[str] = set()
    values: list[int] = []
    candidates = [getattr(result, "best_result", None)]
    candidates.extend(getattr(result, "all_results", None) or [])
    for item in candidates:
        text = str(getattr(item, "text", "") or "")
        if not text or text in seen:
            continue
        seen.add(text)
        seconds = parse_remaining_seconds(text)
        if seconds is not None:
            values.append(seconds)

    if not values:
        log.info("剩余时间读数：ROI 内没有合法的 NN:NN:NN")
        return None
    return min(values)


def hint_dir() -> Path:
    override = os.environ.get("JOB_NEXT_DIR")
    return Path(override) if override else PROJECT_ROOT / "config" / "job_next"


def _account_id() -> str:
    configured = os.environ.get("MAA_INSTANCE_CONFIG")
    if configured:
        return Path(configured).stem
    instance_id = os.environ.get("MFA_INSTANCE_ID", "").strip()
    return instance_id or "default"


def hint_path(account: str | None = None) -> Path:
    return hint_dir() / f"{account or _account_id()}.json"


def write_hint(job: str, *, minutes: float | None, source: str = "self",
               swapped: bool | None = None,
               now: datetime | None = None) -> None:
    """原子写入一条 hint；minutes=None 表示「没读到真实剩余时间」，不写 notBefore。"""
    if not job:
        return
    now = now or datetime.now()
    path = hint_path()
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    jobs = data.setdefault("jobs", {})
    payload = {
        "source": source,
        "startedAt": now.isoformat(timespec="seconds"),
    }
    if minutes is not None:
        payload["notBefore"] = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    if swapped is not None:
        payload["swapped"] = bool(swapped)
    jobs[job] = payload

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False)
        os.replace(tmp, path)
        if minutes is None:
            log.info("NextRun 自报 %s：未读到游戏剩余时间，交由壳走退路（swapped=%s）", job, swapped)
        else:
            log.info("NextRun 自报 %s：+%.1f 分钟（source=%s，swapped=%s）", job, minutes, source, swapped)
    except OSError as exc:
        log.warning("NextRun 自报写入失败（不影响任务）：%s", exc)


class JobNextHintAction(CustomAction):
    """pipeline 节点：{job, minutes} 或 {job, read:"chip_panel"} → 写 hint。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            raw = getattr(argv, "custom_action_param", None) or "{}"
            param = json.loads(raw)
            if isinstance(param, str):
                param = json.loads(param)
            if not isinstance(param, dict):
                raise ValueError("custom_action_param 不是对象")
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("NextRun 参数解析失败：%s", exc)
            return False

        job = str(param.get("job") or "")
        if not job:
            return False

        source = str(param.get("source", "self"))
        minutes = param.get("minutes")
        if param.get("read") == "chip_panel":
            seconds = read_panel_remaining(context)
            if seconds is None:
                # 读不到就是读不到：不猜默认 4 小时，交给壳按实测/静态间隔退路。
                minutes = None
            else:
                buffer_seconds = float(param.get("buffer_seconds", COLLECT_BUFFER_SECONDS))
                minutes = (seconds + buffer_seconds) / 60.0
                source = "game"
                log.info("改造中心剩余 %02d:%02d:%02d → NextRun +%.1f 分钟",
                         seconds // 3600, seconds % 3600 // 60, seconds % 60, minutes)
        elif minutes is not None:
            minutes = float(minutes)

        write_hint(
            job,
            minutes=minutes,
            source=source,
            swapped=param.get("swapped"),
        )
        return True


_INSTANCES = [JobNextHintAction()]
