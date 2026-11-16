# -*- coding: utf-8 -*-
"""资源统计单元测试：验证失败不写0、跨账号隔离、多次任务归因。

用法：python tools/test_resource_stats.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

# 用临时目录覆盖数据路径，避免污染真实数据
_TMP = tempfile.mkdtemp(prefix="resource_stats_test_")
os.environ["RESOURCE_STATS_DIR"] = _TMP

import resource_stats as rs  # noqa: E402
from resource_stats import (  # noqa: E402
    ResourceReading,
    record_after,
    record_before,
    parse_ap,
    parse_int,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


def test_parse_functions():
    print("\n[1] 数字解析函数")
    check("parse_int 纯数字", parse_int("1091740") == 1091740)
    check("parse_int 带逗号", parse_int("1,091,740") == 1091740)
    check("parse_int 空串返回None", parse_int("") is None)
    check("parse_int 非数字返回None", parse_int("abc") is None)
    check("parse_int None返回None", parse_int(None) is None)
    check("parse_ap 正常格式", parse_ap("201/240") == {"current": 201, "max": 240})
    check("parse_ap 全角斜杠", parse_ap("200／240") == {"current": 200, "max": 240})
    check("parse_ap 纯数字容错", parse_ap("200") == {"current": 200, "max": None})
    check("parse_ap 无效返回None", parse_ap("abc") is None)


def test_failure_not_zero():
    """识别失败的资源不写 0，差值为 null。"""
    print("\n[2] 识别失败不写 0")
    account = "test_fail_account"

    # before: ap 成功，coin 失败，gem 成功
    before_readings = {
        "ap": ResourceReading("ap", "体力", {"current": 200, "max": 240}, "200/240", 0.99, success=True),
        "coin": ResourceReading("coin", "金币", None, "", 0.3, success=False),
        "gem": ResourceReading("gem", "粲晶", 5000, "5000", 0.99, success=True),
    }
    before = record_before(account, "DailyRewards", "run_fail_1", before_readings)

    # after: ap 成功，coin 成功，gem 成功
    after_readings = {
        "ap": ResourceReading("ap", "体力", {"current": 198, "max": 240}, "198/240", 0.99, success=True),
        "coin": ResourceReading("coin", "金币", 1092740, "1092740", 0.99, success=True),
        "gem": ResourceReading("gem", "粲晶", 5003, "5003", 0.99, success=True),
    }
    after = record_after(account, "DailyRewards", "run_fail_1", before, after_readings)

    delta = after["delta"]
    check("ap 差值正确", delta["ap"] == -2)
    check("coin 因 before 失败差值为 null", delta["coin"] is None)
    check("gem 差值正确", delta["gem"] == 3)

    # 验证 JSONL 中 coin 的 before 值确实是 null 而非 0
    path = rs.jsonl_path(account)
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    before_line = json.loads(lines[0])
    check("before.coin.value 为 null", before_line["resources"]["coin"]["value"] is None)
    check("before.coin.success 为 false", before_line["resources"]["coin"]["success"] is False)


def test_account_isolation():
    """两个账号数据不混淆。"""
    print("\n[3] 跨账号数据隔离")
    acc_a = "test_account_A"
    acc_b = "test_account_B"

    readings_a = {
        "ap": ResourceReading("ap", "体力", {"current": 100, "max": 240}, "100/240", 0.99, success=True),
        "coin": ResourceReading("coin", "金币", 1000, "1000", 0.99, success=True),
        "gem": ResourceReading("gem", "粲晶", 100, "100", 0.99, success=True),
    }
    readings_b = {
        "ap": ResourceReading("ap", "体力", {"current": 200, "max": 240}, "200/240", 0.99, success=True),
        "coin": ResourceReading("coin", "金币", 2000, "2000", 0.99, success=True),
        "gem": ResourceReading("gem", "粲晶", 200, "200", 0.99, success=True),
    }

    before_a = record_before(acc_a, "DailyRewards", "run_a_1", readings_a)
    before_b = record_before(acc_b, "DailyRewards", "run_b_1", readings_b)

    after_a = readings_a
    after_a = {
        "ap": ResourceReading("ap", "体力", {"current": 90, "max": 240}, "90/240", 0.99, success=True),
        "coin": ResourceReading("coin", "金币", 1500, "1500", 0.99, success=True),
        "gem": ResourceReading("gem", "粲晶", 100, "100", 0.99, success=True),
    }
    after_b = {
        "ap": ResourceReading("ap", "体力", {"current": 190, "max": 240}, "190/240", 0.99, success=True),
        "coin": ResourceReading("coin", "金币", 2500, "2500", 0.99, success=True),
        "gem": ResourceReading("gem", "粲晶", 200, "200", 0.99, success=True),
    }

    rec_a = record_after(acc_a, "DailyRewards", "run_a_1", before_a, after_a)
    rec_b = record_after(acc_b, "DailyRewards", "run_b_1", before_b, after_b)

    check("账号A coin 差值", rec_a["delta"]["coin"] == 500)
    check("账号B coin 差值", rec_b["delta"]["coin"] == 500)
    check("账号A文件不含账号B数据",
          all(json.loads(l)["account"] == acc_a for l in rs.jsonl_path(acc_a).read_text(encoding="utf-8").strip().split("\n")))
    check("账号B文件不含账号A数据",
          all(json.loads(l)["account"] == acc_b for l in rs.jsonl_path(acc_b).read_text(encoding="utf-8").strip().split("\n")))


def test_multi_task_attribution():
    """同一天多次任务，差值各自归因，不复用上一任务余量。"""
    print("\n[4] 多次任务归因")
    account = "test_multi_task"

    # 任务1: coin 1000→1200 (+200)
    b1 = {"coin": ResourceReading("coin", "金币", 1000, "1000", 0.99, success=True),
          "ap": ResourceReading("ap", "体力", {"current": 200, "max": 240}, "200/240", 0.99, success=True),
          "gem": ResourceReading("gem", "粲晶", 500, "500", 0.99, success=True)}
    a1 = {"coin": ResourceReading("coin", "金币", 1200, "1200", 0.99, success=True),
          "ap": ResourceReading("ap", "体力", {"current": 190, "max": 240}, "190/240", 0.99, success=True),
          "gem": ResourceReading("gem", "粲晶", 500, "500", 0.99, success=True)}
    before1 = record_before(account, "DailyRewards", "run_m_1", b1)
    rec1 = record_after(account, "DailyRewards", "run_m_1", before1, a1)
    check("任务1 coin +200", rec1["delta"]["coin"] == 200)

    # 任务2: coin 1200→1250 (+50)，独立配对 run_m_2
    b2 = {"coin": ResourceReading("coin", "金币", 1200, "1200", 0.99, success=True),
          "ap": ResourceReading("ap", "体力", {"current": 190, "max": 240}, "190/240", 0.99, success=True),
          "gem": ResourceReading("gem", "粲晶", 500, "500", 0.99, success=True)}
    a2 = {"coin": ResourceReading("coin", "金币", 1250, "1250", 0.99, success=True),
          "ap": ResourceReading("ap", "体力", {"current": 185, "max": 240}, "185/240", 0.99, success=True),
          "gem": ResourceReading("gem", "粲晶", 510, "510", 0.99, success=True)}
    before2 = record_before(account, "FreeGift", "run_m_2", b2)
    rec2 = record_after(account, "FreeGift", "run_m_2", before2, a2)
    check("任务2 coin +50", rec2["delta"]["coin"] == 50)
    check("任务2 gem +10", rec2["delta"]["gem"] == 10)
    check("任务2 run_id 独立", rec2["run_id"] == "run_m_2")
    check("任务2 before_ts 指向自己的before", rec2["before_ts"] == before2["ts"])


def test_summary_format():
    """日志摘要格式正确。"""
    print("\n[5] 日志摘要格式")
    before = {"ap": {"value": {"current": 201, "max": 240}},
              "coin": {"value": 1091740},
              "gem": {"value": 5816}}
    after = {"ap": {"value": {"current": 200, "max": 240}},
             "coin": {"value": 1092980},
             "gem": {"value": 5819}}
    delta = {"ap": -1, "coin": 1240, "gem": 3}
    summary = rs.format_summary("DailyRewards", delta, before, after)
    check("摘要含金币+1240", "+1240" in summary)
    check("摘要含粲晶+3", "+3" in summary)
    check("摘要含体力201→200", "201→200" in summary)
    print(f"     摘要: {summary}")


def main():
    print(f"测试数据目录: {_TMP}")
    test_parse_functions()
    test_failure_not_zero()
    test_account_isolation()
    test_multi_task_attribution()
    test_summary_format()
    print(f"\n=== 结果: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
