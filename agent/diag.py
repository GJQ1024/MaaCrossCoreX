"""任务级诊断：让「卡在哪一步 / 判据读到什么 / 哪个条件不满足」只靠日志就能回答。

三条通道，各管一层：
- ``logs/task_steps.jsonl``：逐条结构化（stage / judge / fail）。log_archive 按任务
  时间窗把这一段收进 ``logs/task_runs/*.json``，不另起格式。
- ``logs/agent-YYYYMMDD.log``：所有模块的 INFO 及以上。以前只有 arena_loop /
  chip_synth 挂了 handler，其余 40 多个模块的 INFO 全被丢弃（root 默认 WARNING）。
- stderr：只放阶段级摘要，每条任务最多 ``_STAGE_BUDGET`` 行。壳会把 agent stderr
  显示在面板上，逐帧读数不能走这里。

用法：
    diag.stage("进入编队")            # 阶段开始，带一行面板摘要
    diag.done("进入编队", "已识别 3 人")
    diag.judge("遮罩复核", "score<0.8", "score=0.893", ok=False)
    diag.fail("遮罩关闭", actual="第2次点击后 score=0.893", expect="score<0.8",
              frame="debug/on_error/xxx.png")   # 动作返回 False 之前必须调用
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger("laa.diag")

_ROOT = Path(__file__).resolve().parent.parent
_STEP_FILE = _ROOT / "logs" / "task_steps.jsonl"
_STAGE_BUDGET = 10
_MAX_TEXT = 240

_stage_left = _STAGE_BUDGET
_setup_done = False
_details_decoder = json.JSONDecoder()


class _StageOnly(logging.Filter):
    """stderr 的 INFO 通道只放 diag.stage 写的记录，别的一律进文件。"""

    def filter(self, record):
        return bool(getattr(record, "diag_stage", False))


def setup(root=None):
    """进程启动时调用一次：文件日志 + 预算内的阶段摘要。"""
    global _setup_done, _STEP_FILE
    if _setup_done:
        return
    base = Path(root) if root else _ROOT
    _STEP_FILE = base / "logs" / "task_steps.jsonl"
    try:
        (base / "logs").mkdir(parents=True, exist_ok=True)
        if _STEP_FILE.exists() and _STEP_FILE.stat().st_size > 8 * 1024 * 1024:
            # 只在进程启动时滚动；运行中滚动会让 log_archive 的偏移失效。
            _STEP_FILE.replace(_STEP_FILE.with_suffix(".jsonl.1"))
    except OSError:
        return

    logger = logging.getLogger()
    if getattr(logger, "_laa_diag_ready", False):
        _setup_done = True
        return

    fmt = logging.Formatter("[%(asctime)s][%(levelname)s] %(name)s: %(message)s")
    # 细节都进这个文件；原来这些 INFO 因为没 handler 直接丢了。
    try:
        fh = logging.FileHandler(
            base / "logs" / ("agent-%s.log" % datetime.now().strftime("%Y%m%d")),
            encoding="utf-8",
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    stage_stream = logging.StreamHandler(sys.stderr)
    stage_stream.setLevel(logging.INFO)
    stage_stream.addFilter(_StageOnly())
    stage_stream.setFormatter(fmt)
    logger.addHandler(stage_stream)
    # WARNING/ERROR 照旧上面板，保持原来的可见行为。
    warn_stream = logging.StreamHandler(sys.stderr)
    warn_stream.setLevel(logging.WARNING)
    warn_stream.setFormatter(fmt)
    logger.addHandler(warn_stream)
    logger.setLevel(logging.INFO)
    logger._laa_diag_ready = True
    _setup_done = True


def reset_stage_budget():
    """每个任务开始时由 log_archive 调用，保证面板摘要按任务计数。"""
    global _stage_left
    _stage_left = _STAGE_BUDGET


def _brief(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"


def _write(entry):
    entry.setdefault("ts", datetime.now().astimezone().isoformat(timespec="milliseconds"))
    try:
        with open(_STEP_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # 诊断写入失败不得改变控制流
        pass


def stage(name, detail="", result="enter"):
    """阶段进出：进文件，并在预算内上面板一行。"""
    global _stage_left
    _write({"kind": "stage", "stage": name, "result": result, "detail": _brief(detail)})
    verb = "完成" if result == "exit" else "进入"
    if _stage_left > 0:
        _stage_left -= 1
        log.info("[阶段] %s %s%s", verb, name, ("：" + detail) if detail else "",
                 extra={"diag_stage": True})
    else:
        log.debug("[阶段] %s %s %s", verb, name, detail)


def done(name, detail=""):
    stage(name, detail, "exit")


def judge(cond, expect, actual, ok=True, detail=""):
    """单次判据读数：只进文件，不进面板。"""
    _write({"kind": "judge", "cond": cond, "expect": _brief(expect),
            "actual": _brief(actual), "ok": bool(ok), "detail": _brief(detail)})


def fail(cond, actual, expect=None, frame=None, step=None):
    """失败条件：动作返回 False 之前必须调用，缺它就会在归档里标 undiagnosed。"""
    _write({"kind": "fail", "stage": step, "cond": cond, "expect": _brief(expect),
            "actual": _brief(actual), "frame": frame})
    log.error("[失败] %s：%s（期望 %s）", cond, actual, expect)


def _extract_details(line):
    idx = line.find("[details=")
    if idx < 0:
        return None
    brace = line.find("{", idx)
    if brace < 0:
        return None
    try:
        obj, _ = _details_decoder.raw_decode(line, brace)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _trim_reco(reco):
    if not isinstance(reco, dict):
        return reco
    out = {"algorithm": reco.get("algorithm"), "box": reco.get("box")}
    detail = reco.get("detail")
    if isinstance(detail, dict):
        trimmed = {}
        for key in ("all", "filtered", "best", "text", "score", "count"):
            if key in detail:
                value = detail[key]
                trimmed[key] = value[:5] if isinstance(value, list) else value
        out["detail"] = trimmed
    return out


def _reco_text(reco):
    """把一次识别失败压成一行「读到了什么」。"""
    if not isinstance(reco, dict):
        return "无识别详情"
    detail = reco.get("detail") or {}
    if detail.get("best"):
        best = detail["best"]
        return "best=%s" % json.dumps(best, ensure_ascii=False)[:120]
    rows = detail.get("all") or []
    parts = []
    for row in rows[:3]:
        if not isinstance(row, dict):
            continue
        score = row.get("score")
        text = row.get("text")
        if text:
            parts.append("%s(%.3f)" % (text, score or 0.0))
        else:
            parts.append("score=%.3f box=%s" % (score or 0.0, row.get("box")))
    return "；".join(parts) if parts else "无匹配（all 为空）"


def _scan_segment(segment_path, node):
    """从 maafw 归档原文里找该节点最后一次识别详情。"""
    if not segment_path:
        return None
    try:
        with open(segment_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    needle = "node=%s" % node if node else ""
    found = None
    for line in lines[-4000:]:
        if needle and needle not in line:
            continue
        if "Node.Recognition" not in line and "reco_details" not in line:
            continue
        details = _extract_details(line)
        if not details:
            continue
        reco = details.get("reco_details") or details.get("reco") or {}
        if reco:
            found = {"ts": line[1:24], "node": details.get("name") or node,
                     "reco": _trim_reco(reco)}
    return found


def backfill(record, segment_path=None):
    """给没有 diag 记录的任务补「三问」最低标准，返回新字段（原地写入 record）。

    - ① 卡在哪一步：最后一个超时/失败节点；
    - ② 读到了什么：同节点最后一次识别详情；没有就用同前缀兄弟节点，再退到
      maafw 归档原文里扫；
    - ③ 哪个条件不满足：超时（时限内未命中）或动作 success=false。
    截图：有 on_error 帧就带上，但会标明「留帧发生在失败之后」。
    """
    result = {"failure_step": None, "unmet": [], "evidence": [],
              "diagnosis_source": "raw_log"}
    timeouts = record.get("timeouts") or []
    node_failures = record.get("node_failures") or []
    node = None
    unmet = None
    if timeouts:
        last = timeouts[-1]
        node = last.get("node")
        unmet = {"cond": "识别超时", "expect": "%s 在 %sms 内命中" % (node, last.get("timeout_ms")),
                 "actual": None}
    elif node_failures:
        last = node_failures[-1]
        node = last.get("node")
        action = last.get("action") or {}
        unmet = {"cond": "节点动作失败",
                 "expect": "动作 success=true",
                 "actual": "action=%s success=%s" % (action.get("name"), action.get("success"))}

    failures = record.get("recognition_failures") or []
    hits = []
    if node:
        same = [item for item in failures if item.get("node") == node]
        if same:
            hits = same[-1:]
        if not hits:
            prefix = node.rsplit("_", 1)[0]
            near = [item for item in failures if (item.get("node") or "").startswith(prefix)]
            hits = near[-3:]
    if not hits and failures:
        hits = failures[-1:]
    if not hits and node:
        single = _scan_segment(segment_path, node)
        if single:
            hits = [single]
    for item in hits:
        result["evidence"].append({"kind": "recognition", "ts": item.get("ts"),
                                   "node": item.get("node") or node,
                                   "reco": _trim_reco(item.get("reco"))})
    if hits and unmet is not None:
        last = hits[-1]
        unmet["actual"] = "%s：%s" % (last.get("node") or node, _reco_text(last.get("reco")))

    frame = record.get("on_error_frame")
    if frame:
        result["evidence"].append({"kind": "frame", "path": frame,
                                   "note": "留帧发生在判定失败之后，画面可能已是下一屏"})
    if unmet is not None:
        unmet["actual"] = unmet.get("actual") or "无识别记录（节点未执行识别或日志被截断）"
        result["unmet"].append(unmet)
    result["failure_step"] = node
    return result
