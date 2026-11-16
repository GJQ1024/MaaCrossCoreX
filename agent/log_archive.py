"""maafw.log 归档器：让每个任务的失败证据不被后续任务覆盖。

背景（P-065 前置项）：MaaFramework 会在部分任务开始时重建 `debug/maafw.log`
（旧文件改名为 `maafw.bak.<时间>.log`，且旧 bak 可能被清理），导致前一个任务
的节点识别分数、动作结果、失败原因在跑完整队后查不到。壳层（MFAAvalonia）
不可控，因此改由 agent 侧启动一个守护线程：
1. 增量 tail `debug/maafw.log`，写入 `logs/maafw_archive/` 下**只追加不覆盖**的分段文件；
2. 扫描 `debug/maafw.bak.*.log`，把尚未归档的备份整份复制进归档目录；
3. 解析 Tasker.Task.* / Node.* 事件，按任务落盘 `logs/task_runs/*.json` 与
   `logs/task_index.jsonl`；任务失败时追加一行到 `logs/task_failures.log`。

只做观测，不改变任务行为。
"""

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

_POLL_INTERVAL = 0.5
_BAK_SCAN_INTERVAL = 10.0
_SOURCE_HEAD_BYTES = 256

_TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]")
_MSG_RE = re.compile(r"\[msg=([A-Za-z0-9_.]+)\]")
_TIMEOUT_RE = re.compile(r"Task timeout \[pretask\.name=([^\]]+)\].*?\[duration_since\(start_clock\)=(\d+)ms\]")
_ONERROR_RE = re.compile(r"save on error to (.+?\.png)\s*$")

_decoder = json.JSONDecoder()


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _extract_details(line):
    """从一行日志里取出 `[details=...]` 的 JSON；失败返回 None。"""
    idx = line.find("[details=")
    if idx < 0:
        return None
    brace = line.find("{", idx)
    if brace < 0:
        return None
    try:
        obj, _ = _decoder.raw_decode(line, brace)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _trim_reco(reco):
    """识别结果里 all/filtered 可能很长，只留前若干条，保留分数与文本。"""
    if not isinstance(reco, dict):
        return reco
    out = {
        "algorithm": reco.get("algorithm"),
        "box": reco.get("box"),
    }
    detail = reco.get("detail")
    if isinstance(detail, dict):
        trimmed = {}
        for key in ("all", "filtered", "best", "text", "score", "count"):
            if key not in detail:
                continue
            value = detail[key]
            if isinstance(value, list):
                trimmed[key] = value[:8]
            else:
                trimmed[key] = value
        out["detail"] = trimmed
    return out


class _TaskRecord:
    __slots__ = (
        "entry", "task_id", "start", "end", "result", "node_failures",
        "recognition_failures", "timeouts", "on_error", "segment",
    )

    def __init__(self, entry, task_id, ts, segment):
        self.entry = entry
        self.task_id = task_id
        self.start = ts
        self.end = None
        self.result = None
        self.node_failures = []
        self.recognition_failures = []
        self.timeouts = []
        self.on_error = None
        self.segment = segment

    def to_dict(self):
        start = self.start or ""
        duration = None
        try:
            if self.start and self.end:
                fmt = "%Y-%m-%d %H:%M:%S.%f"
                duration = round(
                    (datetime.strptime(self.end, fmt) - datetime.strptime(self.start, fmt)).total_seconds(), 3
                )
        except ValueError:
            pass
        failure_reason = None
        if self.node_failures:
            failure_reason = "node_failed:" + self.node_failures[-1].get("node", "")
        if self.timeouts:
            failure_reason = "reco_timeout:" + self.timeouts[-1].get("node", "")
        return {
            "entry": self.entry,
            "task_id": self.task_id,
            "result": self.result,
            "start": self.start,
            "end": self.end,
            "duration_s": duration,
            "failure_reason": failure_reason,
            "on_error_frame": self.on_error,
            "node_failures": self.node_failures,
            "recognition_failures": self.recognition_failures,
            "timeouts": self.timeouts,
            "raw_log": self.segment,
        }


class Archiver:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.source = self.root / "debug" / "maafw.log"
        self.archive_dir = self.root / "logs" / "maafw_archive"
        self.imported_dir = self.archive_dir / "imported"
        self.task_runs_dir = self.root / "logs" / "task_runs"
        self.index_path = self.root / "logs" / "task_index.jsonl"
        self.failures_path = self.root / "logs" / "task_failures.log"
        self.state_path = self.archive_dir / "state.json"

        self._segment_path = None
        self._segment_name = None
        self._offset = 0
        self._first_line = ""
        self._pending = ""
        self._current = None
        self._seen_events = {}
        self._last_bak_scan = 0.0
        self._lock = threading.Lock()
        self._task_seq = 0

    # ---- 路径与状态 -------------------------------------------------------
    def _ensure_dirs(self):
        for path in (self.archive_dir, self.imported_dir, self.task_runs_dir):
            path.mkdir(parents=True, exist_ok=True)

    def _load_state(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._segment_name = data.get("segment")
        self._offset = int(data.get("offset") or 0)
        self._first_line = data.get("first_line") or ""
        if self._segment_name:
            candidate = self.archive_dir / self._segment_name
            if candidate.exists():
                self._segment_path = candidate
            else:
                self._segment_name = None
                self._offset = 0

    def _save_state(self):
        payload = {
            "source": str(self.source),
            "segment": self._segment_name,
            "offset": self._offset,
            "first_line": self._first_line,
        }
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _drain_stale(self):
        """轮转前把旧文件尾部读完。

        2026-11-09 01:25 实机：MAA 在本任务结束时把 maafw.log 改名为
        `maafw.bak.<时间>.log` 并新建空文件，而归档线程还在轮询间隙里。
        旧实现只负责轮转、不读旧文件剩余部分，导致角斗场任务的
        `Tasker.Task.Failed` 落在间隙里丢失 —— task_runs 里整条任务消失。
        这里从旧偏移开始读最新 bak 的剩余部分，把结尾事件补齐。
        """
        try:
            backups = sorted(self.root.joinpath("debug").glob("maafw.bak.*.log"))
        except OSError:
            return
        for bak in reversed(backups):
            try:
                if bak.stat().st_size < self._offset:
                    continue
                with open(bak, "rb") as fh:
                    head = fh.read(_SOURCE_HEAD_BYTES).split(b"\n", 1)[0].decode(
                        "utf-8", "replace"
                    )
                    if head != self._first_line:
                        continue
                    fh.seek(self._offset)
                    chunk = fh.read()
            except OSError:
                continue
            text = self._pending + chunk.decode("utf-8", "replace")
            lines = text.split("\n")
            self._pending = lines.pop()
            self._append_segment("\n".join(lines) + ("\n" if lines else ""))
            for line in lines:
                if line:
                    self._handle_line(line)
            return

    def _rotate(self, first_line):
        if self._current is not None:
            # 兜底：旧文件尾部没能补齐结束事件时，也要留下一条记录，
            # 宁可标成 unknown 也不能让任务凭空消失。
            record = self._current
            record.end = record.end or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
            record.result = record.result or "unknown"
            self._finalize(record)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._segment_name = "maafw_%s.log" % stamp
        self._segment_path = self.archive_dir / self._segment_name
        self._offset = 0
        self._first_line = first_line
        self._pending = ""
        self._save_state()

    # ---- 归档写入 ---------------------------------------------------------
    def _append_segment(self, text):
        if not text:
            return
        try:
            with open(self._segment_path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
        except OSError:
            pass

    def _import_backups(self):
        try:
            backups = sorted(self.root.joinpath("debug").glob("maafw.bak.*.log"))
        except OSError:
            return
        for bak in backups:
            target = self.imported_dir / bak.name
            if target.exists():
                continue
            try:
                if bak.stat().st_size <= 0:
                    continue
                shutil.copy2(bak, target)
            except OSError:
                continue

    # ---- 事件解析 ---------------------------------------------------------
    def _task_output_path(self, record):
        start = record.start or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stamp = start.replace("-", "").replace(":", "").replace(" ", "_")[:15]
        self._task_seq += 1
        safe_entry = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", record.entry or "task")[:40]
        name = "%s_%03d_%s_%s.json" % (stamp, self._task_seq, safe_entry, record.result or "running")
        return self.task_runs_dir / name

    def _finalize(self, record):
        data = record.to_dict()
        try:
            path = self._task_output_path(record)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            with open(self.index_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if record.result == "failed":
            lines = [
                "[%s] FAILED entry=%s task_id=%s node=%s reason=%s"
                % (
                    record.end or "",
                    record.entry,
                    record.task_id,
                    (data.get("failure_reason") or ""),
                    self._reason_text(record),
                )
            ]
            if record.on_error:
                lines.append("    frame=%s" % record.on_error)
            lines.append("    raw=%s" % record.segment)
            try:
                with open(self.failures_path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            except OSError:
                pass
        self._current = None

    @staticmethod
    def _reason_text(record):
        if record.timeouts:
            last = record.timeouts[-1]
            return "%s 识别超时 %sms" % (last.get("node"), last.get("timeout_ms"))
        if record.node_failures:
            last = record.node_failures[-1]
            action = last.get("action") or {}
            return "动作 %s success=%s" % (action.get("name"), action.get("success"))
        return "未记录到节点级原因"

    def _handle_line(self, line):
        match = _TS_RE.match(line)
        if not match:
            return
        ts = match.group(1)

        # 这两类行没有 [msg=]（WRN / save_on_error），先单独收
        timeout = _TIMEOUT_RE.search(line)
        if timeout and self._current is not None:
            self._current.timeouts.append(
                {"ts": ts, "node": timeout.group(1), "timeout_ms": int(timeout.group(2))}
            )
            return
        on_error = _ONERROR_RE.search(line)
        if on_error and self._current is not None:
            self._current.on_error = on_error.group(1).strip()
            return

        msg_match = _MSG_RE.search(line)
        if not msg_match:
            return
        msg = msg_match.group(1)

        if msg.startswith("Tasker.Task."):
            key = (ts, msg, line[-80:])
            if key in self._seen_events:
                return
            self._seen_events[key] = True
            if len(self._seen_events) > 4000:
                self._seen_events.clear()
        else:
            key = (ts, msg)
            if key in self._seen_events:
                return
            self._seen_events[key] = True
            if len(self._seen_events) > 4000:
                self._seen_events.clear()

        details = _extract_details(line) or {}
        if msg == "Tasker.Task.Starting":
            self._current = _TaskRecord(
                details.get("entry"), details.get("task_id"), ts, self._segment_name
            )
        elif msg in ("Tasker.Task.Succeeded", "Tasker.Task.Failed"):
            record = self._current
            if record is None:
                return
            self._seen_events.pop(key, None)
            record.end = ts
            record.result = "failed" if msg.endswith("Failed") else "succeeded"
            self._finalize(record)
        elif self._current is None:
            return
        elif msg == "Node.PipelineNode.Failed":
            self._current.node_failures.append(
                {
                    "ts": ts,
                    "node": details.get("name"),
                    "node_id": details.get("node_id"),
                    "action": details.get("action_details") or {},
                }
            )
        elif msg == "Node.Recognition.Failed":
            self._current.recognition_failures.append(
                {
                    "ts": ts,
                    "node": details.get("name"),
                    "reco": _trim_reco(details.get("reco_details")),
                }
            )

    # ---- 主循环 -----------------------------------------------------------
    def poll_once(self):
        with self._lock:
            self._poll_source()

    def _poll_source(self):
        try:
            st = self.source.stat()
        except OSError:
            return

        if self._segment_path is None:
            self._rotate("")
            self._save_state()

        first_line = ""
        try:
            with open(self.source, "rb") as fh:
                first_line = fh.read(_SOURCE_HEAD_BYTES).split(b"\n", 1)[0].decode(
                    "utf-8", "replace"
                )
        except OSError:
            return

        if self._first_line and first_line and first_line != self._first_line:
            # 文件被重建（旧内容已改名为 bak），先把旧文件剩余部分读完，再另起分段
            self._drain_stale()
            self._rotate(first_line)
            self._import_backups()
        elif not self._first_line and first_line:
            self._first_line = first_line
            self._save_state()

        if st.st_size < self._offset:
            # 原地截断
            self._drain_stale()
            self._rotate(first_line)
            self._save_state()

        if st.st_size == self._offset:
            return

        try:
            with open(self.source, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return
        self._offset += len(chunk)
        text = self._pending + chunk.decode("utf-8", "replace")
        lines = text.split("\n")
        self._pending = lines.pop()
        self._append_segment("\n".join(lines) + ("\n" if lines else ""))
        for line in lines:
            if line:
                self._handle_line(line)
        self._save_state()

    def run(self):
        self._ensure_dirs()
        self._load_state()
        self._import_backups()
        self._last_bak_scan = time.time()
        while True:
            try:
                self.poll_once()
                if time.time() - self._last_bak_scan >= _BAK_SCAN_INTERVAL:
                    self._import_backups()
                    self._last_bak_scan = time.time()
            except Exception as exc:  # 归档失败不能影响任务
                print("[log_archive] poll error: %r" % (exc,))
            time.sleep(_POLL_INTERVAL)


def start(root=None):
    """启动归档守护线程；重复调用无副作用。"""
    if os.environ.get("MCC_LOG_ARCHIVE_DISABLED") == "1":
        return
    root = Path(root) if root else _root()
    archiver = Archiver(root)

    def _runner():
        try:
            archiver.run()
        except Exception as exc:
            print("[log_archive] stopped: %r" % (exc,))

    thread = threading.Thread(target=_runner, name="maafw-log-archive", daemon=True)
    thread.start()
    print("[log_archive] started, root=%s" % root)
