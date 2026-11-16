from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_reward_mail_switch():
    interface = load("assets/interface.json")
    reward = load("assets/resource/pipeline/base/领取奖励.json")
    # 「领取奖励」已并入带记忆门禁的「领奖_记忆门禁」，邮箱开关仍挂在这个任务上
    task = next(item for item in interface["task"] if item["entry"] == "领奖_记忆门禁")
    option = interface["option"]["是否领取邮箱内容"]

    assert "是否领取邮箱内容" in task["option"]
    assert option["type"] == "switch" and option["default_case"] == "Yes"
    assert option["cases"][0]["pipeline_override"]["领取奖励_跳过邮箱"]["enabled"] is False
    assert option["cases"][1]["pipeline_override"]["领取奖励_跳过邮箱"]["enabled"] is True
    # 跳过邮箱的判断在进邮件之前；跳过后直接转去每日任务确认
    nexts = reward["领取奖励"]["next"]
    assert nexts.index("领取奖励_跳过邮箱") < nexts.index("领奖_锚点检查")
    assert reward["领取奖励_跳过邮箱"]["next"] == [
        "领奖_确认主界面进每日",
        "[JumpBack]子任务_进入首页",
    ]


def test_close_emulator_switch_for_both_resources():
    interface = load("assets/interface.json")
    base = load("assets/resource/pipeline/base/关闭游戏.json")
    bilibili = load("assets/resource/bilibili/pipeline/shutdown.json")
    task = next(item for item in interface["task"] if item["entry"] == "关闭游戏")
    option = interface["option"]["关闭模拟器"]
    expected_next = ["关闭游戏_并关闭模拟器", "关闭游戏_仅关闭游戏"]

    assert "关闭模拟器" in task["option"]
    assert option["type"] == "switch" and option["default_case"] == "Yes"
    # base 版把停游拆成独立的「关闭游戏_停止」节点，再交棒给两个收尾分支；
    # bilibili 版没有中间节点，直接分流。
    assert base["关闭游戏"]["next"] == ["关闭游戏_停止"]
    assert base["关闭游戏_停止"]["action"] == "StopApp"
    assert base["关闭游戏_停止"]["next"] == expected_next
    assert bilibili["关闭游戏"]["next"] == expected_next
    assert base["关闭游戏_并关闭模拟器"]["custom_action"] == "shutdown_mumu"
    yes, no = option["cases"]
    assert yes["pipeline_override"]["关闭游戏_并关闭模拟器"]["enabled"] is True
    assert yes["pipeline_override"]["关闭游戏_仅关闭游戏"]["enabled"] is False
    assert no["pipeline_override"]["关闭游戏_并关闭模拟器"]["enabled"] is False
    assert no["pipeline_override"]["关闭游戏_仅关闭游戏"]["enabled"] is True


def test_order_friend_merged_task():
    """添加+拉黑合并为「订单好友」，入口先检查配置，没填就早退。"""
    interface = load("assets/interface.json")
    add = load("assets/resource/pipeline/base/添加订单好友.json")
    names = [item["name"] for item in interface["task"]]

    assert "订单好友" in names
    assert "添加订单好友" not in names and "拉黑订单好友" not in names

    task = next(item for item in interface["task"] if item["name"] == "订单好友")
    assert task["entry"] == "订单好友_任务"
    assert task["option"] == ["订单好友_好友ID"]

    option = interface["option"]["订单好友_好友ID"]
    assert option["type"] == "input"
    assert option["inputs"][0]["name"] == "好友ID"
    assert option["inputs"][0]["default"] == ""

    gate = add["订单好友_任务"]
    assert gate["action"]["param"]["custom_action"] == "订单好友_前置检查"
    assert gate["next"] == ["订单好友添加流程"]
    # 早退终端：无 next，任务按正常结束
    assert not add["订单好友_跳过"].get("next")
    # 加完后自动进拉黑阶段
    assert add["加好友完毕"]["next"] == ["订单好友_拉黑阶段入口"]
    assert add["订单好友_拉黑阶段入口"]["action"]["param"]["custom_action"] == "订单好友_拉黑检查"
    assert add["订单好友_拉黑阶段入口"]["next"] == ["拉黑好友流程"]


def test_reward_claims_weekly_on_same_page():
    """「领取奖励」在日常领完后顺手领同一页的周常奖励（不新增任务项）。"""
    reward = load("assets/resource/pipeline/base/领取奖励.json")
    interface = load("assets/interface.json")

    # 不新增任务：interface 里没有以本段命名的新入口
    assert all(not item["entry"].startswith("常规任务奖励") for item in interface["task"])

    # 页签复位在使用点：进页面后先显式切日常，避免上一轮停在周常导致日常点错页
    assert reward["领奖_进每日任务"]["next"] == ["每日任务_日常页签"]
    daily_tab = reward["每日任务_日常页签"]
    assert daily_tab["expected"] == ["日常"]
    assert daily_tab["roi"] == [20, 80, 240, 240]
    assert daily_tab["next"] == ["每日任务_阶段"]

    assert reward["每日任务_结束"]["next"] == ["常规任务奖励_页面判据"]

    # 入口自足：页面判据失败 → 自导航回任务页 → 点任务（带回首页兜底）
    gate = reward["常规任务奖励_页面判据"]
    assert gate["recognition"] == "OCR" and gate["expected"] == ["常规任务"]
    assert gate["roi"] == [20, 150, 180, 300]
    assert gate["on_error"] == ["常规任务奖励_回任务页"]
    assert reward["常规任务奖励_回任务页"]["next"] == [
        "常规任务奖励_点任务",
        "[JumpBack]子任务_进入首页",
    ]
    assert reward["常规任务奖励_点任务"]["expected"] == ["任务"]
    assert reward["常规任务奖励_点任务"]["next"] == ["常规任务奖励_页面判据"]

    # 只补周常：日常已由 每日任务_一键领取 覆盖，剧情任务整段已删除
    assert reward["常规任务奖励_周常页签"]["expected"] == ["周常"]
    assert reward["常规任务奖励_周常页签"]["roi"] == [20, 300, 160, 40]
    assert reward["常规任务奖励_周常确认"]["roi"] == [650, 185, 560, 110]
    assert "常规任务奖励_剧情页签" not in reward
    assert "常规任务奖励_剧情一键领取" not in reward

    # 领取判据：点完必须由通用遮罩命中「获得物品」；按钮判据复用每日任务那份
    node = reward["常规任务奖励_周常一键领取"]
    assert node["next"][0] == "[JumpBack]通用-遮罩处理"
    assert node["max_hit"] == 2
    btn = node["recognition"]["param"]["all_of"][0]["recognition"]["param"]
    assert btn["template"] == ["一键领取.png"]
    assert btn["roi"] == [1000, 60, 260, 90]

    # 无奖可领 → 跳过并正常收尾，仍回到原通行证链路
    assert node["on_error"] == ["常规任务奖励_结束"]
    assert reward["常规任务奖励_周常页签"]["on_error"] == ["常规任务奖励_结束"]
    assert reward["常规任务奖励_周常确认"]["on_error"] == ["常规任务奖励_结束"]
    assert reward["常规任务奖励_结束"]["next"] == ["领奖_每日完转通行证"]


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"TASK_OPTION_FLOWS_OK ({len(tests)} tests)")
