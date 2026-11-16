# -*- coding: utf-8 -*-
"""任务记忆（agent/task_memory.py）离线验证：7 个场景，无需设备。

用法：
    python tools/test_task_memory.py

覆盖：空记录放行 / 到点拦截 / 跨日重置 / 上限用满 / 强制只无视冷却 /
状态文件损坏恢复 / 时钟回拨。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

CLONE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CLONE_ROOT / "agent"))

_TMP = tempfile.mkdtemp(prefix="task_memory_test_")
os.environ["TASK_MEMORY_DIR"] = _TMP  # 必须在 import task_memory 前设置

import task_memory as tm  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, 0)
POLICY = tm.TaskPolicy(cooldown_minutes=30, daily_limit=1, refresh_hour=5)


def fresh_store(name="acc"):
    path = Path(_TMP) / f"{name}.json"
    if path.exists():
        path.unlink()
    return tm.TaskMemoryStore(path), path


def argv(param, double_encode=False):
    raw = json.dumps(param, ensure_ascii=False)
    if double_encode:
        raw = json.dumps(raw, ensure_ascii=False)
    return SimpleNamespace(custom_action_param=raw)


def test_empty_record_allows():
    store, _ = fresh_store("s1")
    d = store.check("DailyRewards", POLICY, NOW)
    assert d.allowed and not d.reasons, f"空记录应放行: {d.reasons}"
    print("PASS 1 空记录放行")


def test_cooldown_and_limit_block():
    store, _ = fresh_store("s2")
    store.mark_completed("DailyRewards", POLICY, NOW)
    d = store.check("DailyRewards", POLICY, NOW + timedelta(minutes=10))
    assert not d.allowed
    assert set(d.reasons) == {"cooldown", "limit"}, f"冷却+上限应同时给出: {d.reasons}"
    assert d.next_available_at, "拦截应给出下次可跑时间"
    print("PASS 2 到点拦截（冷却+上限双原因）")


def test_cross_day_rollover():
    store, _ = fresh_store("s3")
    store.mark_completed("DailyRewards", POLICY, NOW)
    nxt = datetime(2026, 11, 2, 5, 1)  # 次日刷新点之后
    d = store.check("DailyRewards", POLICY, nxt)
    assert d.allowed, f"跨游戏日应重置放行: {d.reasons}"
    st = store.snapshot()["tasks"]["DailyRewards"]
    assert st["day_id"] == "2026-11-02" and st["today_count"] == 0
    # 刷新点边界：04:59 还算前一天
    assert tm.game_day_id(datetime(2026, 11, 2, 4, 59), 5) == "2026-11-01"
    print("PASS 3 跨日重置（05:00 边界正确）")


def test_limit_exhausted_after_cooldown():
    store, _ = fresh_store("s4")
    store.mark_completed("DailyRewards", POLICY, NOW)
    d = store.check("DailyRewards", POLICY, NOW + timedelta(minutes=31))
    assert not d.allowed and d.reasons == ["limit"], \
        f"冷却过了但次数满应只报 limit: {d.reasons}"
    print("PASS 4 上限用满（原因仅 limit）")


def test_force_bypasses_cooldown_only():
    store, _ = fresh_store("s5")
    store.mark_completed("DailyRewards", POLICY, NOW)
    d = store.check("DailyRewards", POLICY, NOW + timedelta(minutes=10), force=True)
    assert not d.allowed and d.reasons == ["limit"], \
        f"强制只应无视冷却，上限仍拦: {d.reasons}"
    # 次数未满时 force 应放行
    store2, _ = fresh_store("s5b")
    store2.mark_completed("T", tm.TaskPolicy(cooldown_minutes=30, daily_limit=0), NOW)
    d2 = store2.check("T", tm.TaskPolicy(cooldown_minutes=30, daily_limit=0),
                      NOW + timedelta(minutes=10), force=True)
    assert d2.allowed, "冷却中+无上限+force 应放行"
    print("PASS 5 强制只无视冷却，上限仍拦")


def test_corrupt_state_file_recovers():
    store, path = fresh_store("s6")
    store.mark_completed("DailyRewards", POLICY, NOW)
    path.write_text("{broken json", encoding="utf-8")
    store2 = tm.TaskMemoryStore(path)  # 损坏应备份重建而不是炸掉
    d = store2.check("DailyRewards", POLICY, NOW)
    assert d.allowed, "损坏重建后应按空记录放行"
    assert path.with_suffix(".json.corrupt").exists(), "损坏文件应备份为 .corrupt"
    print("PASS 6 状态文件损坏恢复（备份 .corrupt 后重建）")


def test_clock_rollback():
    store, path = fresh_store("s7")
    # 人为写入"未来"的完成时间（模拟时钟回拨前的记录）
    future = (NOW + timedelta(days=1)).isoformat(timespec="seconds")
    path.write_text(json.dumps({
        "version": 1, "account": "s7",
        "tasks": {"DailyRewards": {
            "last_completed_at": future, "today_count": 1,
            "day_id": tm.game_day_id(NOW, 5), "last_result": "succeeded",
        }},
    }, ensure_ascii=False), encoding="utf-8")
    store2 = tm.TaskMemoryStore(path)
    d = store2.check("DailyRewards", tm.TaskPolicy(cooldown_minutes=30, daily_limit=9), NOW)
    assert "cooldown" not in d.reasons, \
        f"时钟回拨不该因'未来的完成时间'被冷却拦截: {d.reasons}"
    print("PASS 7 时钟回拨（不按未来记录拦截，日志告警）")


def test_actions_end_to_end():
    """gate/complete 动作本体：含双重编码 param 与强引用实例。"""
    os.environ["TASK_MEMORY_DIR"] = _TMP
    gate = tm.TaskMemoryGateAction()
    complete = tm.TaskMemoryCompleteAction()
    held = {type(i) for i in tm._INSTANCES}
    assert tm.TaskMemoryGateAction in held and tm.TaskMemoryCompleteAction in held, \
        "模块级必须强引用两个动作实例（防 GC 回调悬空）"
    param = {"task": "DailyRewards", "daily_limit": 1}
    assert gate.run(None, argv(param, double_encode=True)), "空记录 gate 应放行"
    assert complete.run(None, argv(param, double_encode=True)), "complete 应写记录"
    assert not gate.run(None, argv(param, double_encode=True)), "达上限 gate 应拦截"
    print("PASS 8 动作本体（双重编码 param + 强引用）")


if __name__ == "__main__":
    test_empty_record_allows()
    test_cooldown_and_limit_block()
    test_cross_day_rollover()
    test_limit_exhausted_after_cooldown()
    test_force_bypasses_cooldown_only()
    test_corrupt_state_file_recovers()
    test_clock_rollback()
    test_actions_end_to_end()
    print("\nALL 8 CHECKS PASSED")
