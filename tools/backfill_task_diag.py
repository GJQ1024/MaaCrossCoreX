"""用已有归档回补「三问」，不重跑真机。

输入：``logs/task_runs/*.json``（历史任务记录）+ 它引用的 ``logs/maafw_archive/<raw_log>``。
输出：``logs/diagnosis_samples/<原文件名>.json``，字段与在线归档一致
（failure_step / unmet / evidence / stages / diagnosis_complete / diagnosis_source），
原记录不改。

用法：
    python tools/backfill_task_diag.py 20261112_113730_005_角斗场_failed.json ...
    python tools/backfill_task_diag.py --all-failed
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "B"
sys.path.insert(0, str(DIST / "agent"))

import diag  # noqa: E402


def _stages_from_record(record):
    """历史记录没有 task_steps，用节点事件拼一条最朴素的阶段链。"""
    stages = []
    for item in record.get("node_failures") or []:
        stages.append({"ts": item.get("ts"), "kind": "stage", "stage": item.get("node"),
                       "result": "failed", "detail": "节点失败"})
    for item in record.get("timeouts") or []:
        stages.append({"ts": item.get("ts"), "kind": "stage", "stage": item.get("node"),
                       "result": "timeout", "detail": "识别超时 %sms" % item.get("timeout_ms")})
    stages.sort(key=lambda x: x.get("ts") or "")
    return stages


def backfill_one(path):
    path = Path(path)
    record = json.loads(path.read_text(encoding="utf-8"))
    segment = None
    if record.get("raw_log"):
        candidate = DIST / "logs" / "maafw_archive" / record["raw_log"]
        segment = candidate if candidate.exists() else None
    filled = diag.backfill(record, segment)
    stages = record.get("stages") or _stages_from_record(record)
    failure_step = record.get("failure_step") or filled["failure_step"]
    unmet = record.get("unmet") or filled["unmet"]
    evidence = record.get("evidence") or filled["evidence"]
    kind = "failure" if record.get("result") == "failed" else "succeeded"
    readings = record.get("recognition_failures") or []
    if record.get("result") != "failed" and not unmet and readings:
        # 静默成功：任务判过但一次产出都没有（P-105），三问照样要能回答。
        last = readings[-1]
        kind = "silent_success"
        failure_step = failure_step or last.get("node")
        unmet = [{
            "cond": "自定义识别返回空但仍判成功",
            "expect": "至少产出 1 个字段，或明确报告「本屏无可读对象」",
            "actual": "整个任务 %.1fs，%d 次识别全空（all 只有 box=[0,0,0,0]），无产出日志"
                      % (record.get("duration_s") or 0, len(readings)),
        }]
    if not failure_step and stages:
        failure_step = stages[-1].get("stage")
    complete = bool(failure_step and unmet and evidence)
    out = {
        "entry": record.get("entry"),
        "result": record.get("result"),
        "diagnosis_kind": kind,
        "start": record.get("start"),
        "end": record.get("end"),
        "duration_s": record.get("duration_s"),
        "failure_reason": record.get("failure_reason"),
        "failure_step": failure_step,
        "diagnosis_complete": complete,
        "diagnosis_source": "backfill:raw_log",
        "stages": stages,
        "unmet": unmet,
        "evidence": evidence,
        "on_error_frame": record.get("on_error_frame"),
        "raw_log": record.get("raw_log"),
    }
    target_dir = DIST / "logs" / "diagnosis_samples"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return target, out


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    if argv == ["--all-failed"]:
        paths = [p for p in (DIST / "logs" / "task_runs").glob("*_failed.json")]
    else:
        paths = [DIST / "logs" / "task_runs" / name for name in argv]
    for path in paths:
        if not path.exists():
            print("missing:", path)
            continue
        target, out = backfill_one(path)
        print("==", path.name, "->", target)
        print("   卡在哪一步:", out["failure_step"])
        for item in out["unmet"]:
            print("   哪个条件:", item.get("cond"), "| 期望:", item.get("expect"),
                  "| 实际:", item.get("actual"))
        for ev in out["evidence"][:3]:
            print("   读到什么:", json.dumps(ev, ensure_ascii=False)[:260])
        print("   三问齐全:", out["diagnosis_complete"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
