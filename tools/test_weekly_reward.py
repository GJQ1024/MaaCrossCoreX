"""周本·活动探索与挑战流程的结构锁定。

2026-11-09 旧链清理（A 方案，见 `docs/问题登记.md` P-079）后：
- 碎星虚影本周报酬改由独立任务 `碎星虚影.json`（StarShadow 记忆门禁）负责；
- `周本.json` 里旧的「点卡片领奖 → 奖励领取页 → 点外边缘 → 左滑找回 → 读取报酬」整套
  11 个节点已删除（领奖 UI 已变成点卡片直接弹「获得物品」，中间页不存在）；
- `活动探索.next` 只保留 `周本_领完即结束`，即领完/无奖励直接收尾；
- `interface.json` 的 `周本_领奖后处理` 选项随链一起移除。
- 2026-11-11 补回周常领取：`活动探索.next` 改为「进页弹窗 / 碎星虚影卡片 / 领完即结束」三路，
  成功证据只认「获得物品」弹窗（P-079/P-084 实测），点卡片不算成功。

本测试锁定：新结构、旧节点彻底消失、挑战链本体未被误伤、以及全仓引用可解析。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = ROOT / "assets" / "resource" / "pipeline"
WEEKLY = PIPELINE_DIR / "base" / "周本.json"

# 实测：600/600 的框（1280 基准），仍由 `活动探索_已存在` 覆盖
MEASURED_BOX = (578, 219, 110, 32)
REWARD_ROI = [20, 185, 1240, 100]

# 2026-11-09 A 方案删掉的 11 个节点
LEGACY_NODES = [
    "活动探索_碎星虚影",
    "周本_碎星虚影_应有位置",
    "周本_碎星虚影_分派",
    "周本_点击碎星虚影_领奖",
    "周本_奖励领取页_点外边缘",
    "周本_领奖后_判定",
    "周本_碎星虚影_原位置仍在",
    "周本_左滑找碎星虚影",
    "周本_碎星虚影_滑动后已找到",
    "周本_读取报酬",
    "周本_读不到报酬也去点碎星虚影",
]


def _load(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _next(node):
    value = node.get("next")
    return value if isinstance(value, list) else ([value] if value else [])


def test_reward_option_removed_from_task():
    """「领奖后」下拉随旧链一起删除，周本任务只剩关卡选择与强制执行。"""
    interface = _load(ROOT / "assets" / "interface.json")
    assert "周本_领奖后处理" not in interface["option"], "旧选项定义没删干净"
    task = next(t for t in interface["task"] if t["name"] == "周本")
    assert task["option"] == ["周本关卡选择", "本次强制执行"], task["option"]


def test_activity_explore_claims_weekly_reward_first():
    """进入活动探索页先领周常（有可领才点），再走「领完即结束」。"""
    weekly = _load(WEEKLY)
    assert _next(weekly["活动探索"]) == [
        "周本_周常奖励_进页弹窗",
        "周本_周常奖励_卡片",
        "周本_领完即结束",
    ], _next(weekly["活动探索"])
    assert _next(weekly["周本_领完即结束"]) == ["周本_记忆完成"]
    # 「点了」不算成功：唯一成功证据是「获得物品」弹窗
    for name in ("周本_周常奖励_进页弹窗", "周本_周常奖励_获得物品"):
        assert weekly[name]["recognition"]["param"]["expected"] == ["获得物品"], name
        assert _next(weekly[name]) == ["周本_领完即结束"], name
    # 点卡片后必须再判状态，不能直接当成功
    assert _next(weekly["周本_周常奖励_卡片"]) == [
        "周本_周常奖励_获得物品",
        "周本_周常奖励_进入挑战页",
        "周本_领完即结束",
    ], _next(weekly["周本_周常奖励_卡片"])


def test_legacy_claim_chain_removed():
    """旧领奖链 11 个节点必须全部消失，且没有任何节点还指向它们。"""
    weekly = _load(WEEKLY)
    for name in LEGACY_NODES:
        assert name not in weekly, f"旧节点未删除：{name}"

    dangling = []
    for key, node in weekly.items():
        if not isinstance(node, dict):
            continue
        for field in ("next", "on_error"):
            value = node.get(field)
            value = value if isinstance(value, list) else ([value] if value else [])
            for target in value:
                if not isinstance(target, str):
                    continue
                target = re.sub(r"^\[(JumpBack|Anchor)\]", "", target)
                if target in LEGACY_NODES:
                    dangling.append(f"{key}.{field} -> {target}")
    assert not dangling, "仍指向已删节点：" + "; ".join(dangling)


def test_weekly_challenge_structure_intact():
    """周本挑战链本体（次数计数、两关分派、BOSS 页）不因清理改动。"""
    interface = _load(ROOT / "assets" / "interface.json")
    weekly = _load(WEEKLY)

    count = interface["option"]["周本_挑战次数"]
    assert count["default_case"] == "1"
    assert [case["name"] for case in count["cases"]] == [str(n) for n in range(1, 11)]

    assert _next(weekly["周本"]) == ["周本_次数初始化"]
    assert weekly["周本_次数初始化"]["custom_action"] == "weekly_run_count"
    assert weekly["周本_次数已达"]["custom_recognition"] == "weekly_run_reached"

    for stage, return_node, battle_node in (
        (0, "周本_挑战完成_阿瑞斯", "活动探索_虚影阿瑞斯"),
        (1, "周本_挑战完成_宙斯", "活动探索_第五关"),
    ):
        overrides = interface["option"]["周本关卡选择"]["cases"][stage]["pipeline_override"]
        assert return_node in _next(overrides["活动探索_战斗结束3"])
        assert _next(weekly[return_node]) == ["周本_次数分派"]
        assert weekly[return_node]["custom_action_param"]["operation"] == "complete"
        branch = overrides.get("周本_次数分派", weekly["周本_次数分派"])
        assert _next(branch) == ["周本_次数已达", battle_node]

    boss = weekly["周本_BOSS选择页_已进入"]
    assert boss["action"] == "DoNothing"
    assert "尼克罗虚影" in boss["expected"]
    assert _next(boss) == ["活动探索_戈里刻虚影", "周本_BOSS选择页_找戈里刻"]
    golike = weekly["活动探索_戈里刻虚影"]
    assert golike["recognition"]["param"]["expected"] == ["戈里刻虚影"]
    assert "target" not in golike["action"], "必须点击 OCR 识别出的文字框"


def test_reward_box_roi_covers_measured_position():
    """报酬框 ROI（活动探索页顶部带）必须仍盖住实测的 600/600 框。"""
    weekly = _load(WEEKLY)
    node = weekly["活动探索_已存在"]
    param = node["recognition"]["param"] if isinstance(node["recognition"], dict) else node
    x, y, w, h = MEASURED_BOX
    roi = param["roi"]
    assert roi[0] <= x and roi[0] + roi[2] >= x + w, f"横向没盖住报酬框：{roi}"
    assert roi[1] <= y and roi[1] + roi[3] >= y + h, f"纵向没盖住报酬框：{roi}"
    assert "600/600" in param["expected"], param["expected"]
    assert roi == REWARD_ROI


def test_all_pipeline_references_resolve():
    """全仓管道里所有 next/on_error 目标都必须能解析到已定义节点。"""
    names = set()
    files = []
    for path in PIPELINE_DIR.rglob("*.json"):
        try:
            data = _load(path)
        except Exception:
            continue
        files.append(data)
        names.update(k for k, v in data.items() if isinstance(v, dict) and not k.startswith("$__mpe_config"))

    missing = []
    for data in files:
        for key, node in data.items():
            if not isinstance(node, dict):
                continue
            for field in ("next", "on_error"):
                value = node.get(field)
                if not isinstance(value, list):
                    value = [value] if value else []
                for target in value:
                    if not isinstance(target, str):
                        continue
                    target = re.sub(r"^\[(JumpBack|Anchor)\]", "", target)
                    if target and target not in names:
                        missing.append(f"{key}.{field} -> {target}")
    assert not missing, "悬空引用：" + "; ".join(sorted(set(missing))[:10])


def test():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for case in tests:
        case()
    print(f"WEEKLY_REWARD_OK ({len(tests)} tests)")


if __name__ == "__main__":
    test()
