# -*- coding: utf-8 -*-
"""启动任务待机页面（游戏屏保）唤醒接线的离线回归测试。"""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_idle_clock_recognizer_is_shared_across_nodes():
    main_ui = load("assets/resource/pipeline/base/main_ui.json")
    startup = load("assets/resource/pipeline/base/启动游戏.json")

    shared = main_ui["NavIdleMainClock"]
    wake = startup["启动_唤醒待机页面"]
    assert shared["recognition"] == wake["recognition"] == "OCR"
    # 主界面时钟用全匹配；待机屏保时钟带「A.M」后缀（2026-11-09 01:41 实机帧），
    # 唤醒节点去掉尾部 $ 才能命中，属有意不一致。
    assert shared["expected"] == [r"^[0-2]?\d\s*[:：.]\s*[0-5]\d$"]
    assert wake["expected"] == [r"^[0-2]?\d\s*[:：.]\s*[0-5]\d"]

    # 唤醒节点的 ROI 必须覆盖左下角时钟布局；上游 [320,140,640,440] 漏这一种，
    # 2026-11-01 实机因此超时，故 rois 允许不同但必须包住左下角。
    x, y, w, h = wake["roi"]
    assert x == 0
    assert y <= 480 and y + h >= 640


def test_startup_wakes_game_screensaver_with_neutral_point():
    startup = load("assets/resource/pipeline/base/启动游戏.json")
    entry_next = startup["进入首页"]["next"]
    assert entry_next.index("启动_唤醒待机页面") < entry_next.index("[JumpBack]启动游戏")

    wake = startup["启动_唤醒待机页面"]
    assert wake["action"] == "Click"
    # 不写绝对 target（coords.md §4）：点击 OCR 命中的时钟框中心，时钟是纯显示区
    assert "target" not in wake
    assert wake["next"] == ["进入首页"]
    assert 1 <= wake["max_hit"] <= 3
    assert wake["post_delay"] >= 1500


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print("STARTUP_IDLE_OK (%d tests)" % len(tests))
