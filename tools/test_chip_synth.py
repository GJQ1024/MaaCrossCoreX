# -*- coding: utf-8 -*-
"""Offline tests for chip synthesis thresholds, icon long-press and currency ledger."""

from pathlib import Path
from types import SimpleNamespace
import os
import sys

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import chip_synth  # noqa: E402


def _detail(main, subs):
    return {
        "main_skill": {"name": "连击", "level": main},
        "sub_skills": [
            {"name": name, "level": level}
            for name, level in zip(("攻击", "命中", "暴伤"), subs)
        ],
    }


def test_default_threshold_comes_from_interface():
    assert chip_synth._default_threshold() == "2/1/1/1"
    assert chip_synth._parse_threshold(chip_synth._default_threshold()) == [2, 1, 1, 1]
    assert chip_synth._resolve_options({})["qualities"] == ["R5"]
    assert chip_synth._parse_threshold("2/1/1") is None
    assert chip_synth._parse_threshold("2/1/1/x") is None
    assert chip_synth._parse_threshold("9/1/1/1") == [3, 1, 1, 1]


def test_level_equal_to_threshold_counts_as_hit():
    threshold = [2, 1, 1, 1]
    # 档 2 边界：主=阈值、三副全部等于阈值 → 命中
    assert chip_synth._verdict(_detail(2, (1, 1, 1)), threshold) == (
        True, "主2副全≤1/1/1", ""
    )
    # 副技能超出 1 级 → 保留
    hit, tier, reason = chip_synth._verdict(_detail(2, (1, 2, 1)), threshold)
    assert hit is False and tier == "" and "副2" in reason
    # 档 1：主 1 级完全不看副技能 → 命中
    assert chip_synth._verdict(_detail(1, (3, 3, 3)), threshold) == (True, "主1", "")
    # 主技能超出阈值 → 保留
    assert chip_synth._verdict(_detail(3, (1, 1, 1)), threshold)[0] is False


def test_missing_threshold_config_keeps_every_chip():
    hit, tier, reason = chip_synth._verdict(_detail(1, (1, 1, 1)), [])
    assert hit is False and tier == "" and reason == "阈值未配置"


def test_signature_ignores_dynamic_detail_fields():
    base = _detail(2, (1, 1, 1))
    same = dict(base, _lock_toggle_point=(1207, 155))
    assert chip_synth._signature(base) == chip_synth._signature(same)
    changed = _detail(2, (1, 2, 1))
    assert chip_synth._signature(base) != chip_synth._signature(changed)


class _FakeSession:
    def __init__(self, threshold=None):
        self.shots = []
        self.report = {"truncated": False}
        self.options = {
            "threshold": threshold or [2, 1, 1, 1],
            "threshold_raw": "/".join(str(v) for v in (threshold or [2, 1, 1, 1])),
        }

    def shot(self, _context, name):
        self.shots.append(name)
        return name


def _entry():
    return {"skill": "连击", "scanned": 0, "locked_skipped": 0, "notes": []}


def test_icon_point_lands_on_chip_icon():
    """长按落点必须落在芯片图标上：图标实测 x 226-274 / y 194-252（栅格单元中心 254,228）。"""
    point = chip_synth._icon_point({"point": (254, 228)})
    assert point == (250, 223)
    assert 226 <= point[0] <= 274 and 194 <= point[1] <= 252
    for _duration, dx, dy in chip_synth.DETAIL_ATTEMPTS:
        assert abs(dx) <= chip_synth.ICON_HALF[0]
        assert abs(dy) <= chip_synth.ICON_HALF[1]
    # 第二次重试必须换落点，且落点仍在图标内
    _duration, dx, dy = chip_synth.DETAIL_ATTEMPTS[1]
    second = (point[0] + dx, point[1] + dy)
    assert second != point
    assert 226 <= second[0] <= 274 and 194 <= second[1] <= 252


class _FakeSynth:
    def __init__(self, options, dry_run):
        self.options = options
        self.dry_run = dry_run
        self.dir = Path("sandbox/test")
        self.report = {"status": "running"}

    def flush(self):
        pass


def test_init_without_skills_overrides_to_skip():
    action = chip_synth.ChipSynthAction()
    calls = []
    context = SimpleNamespace(override_next=lambda node, nxt: calls.append((node, list(nxt))))
    original_config = chip_synth._config_options
    original_synth = chip_synth._Synth
    chip_synth._config_options = lambda: {"skills": [], "threshold": "2/1/1/1"}
    chip_synth._Synth = _FakeSynth
    try:
        ok_skip = action._init(context, None, {"dry_run": True})
        skip_status = chip_synth._SESSION["status"]
        chip_synth._config_options = lambda: {"skills": ["连击"], "threshold": "2/1/1/1"}
        ok_ready = action._init(context, None, {"dry_run": True})
        ready_status = chip_synth._SESSION["status"]
    finally:
        chip_synth._config_options = original_config
        chip_synth._Synth = original_synth

    assert ok_skip is True and skip_status == "skip"
    assert calls == [("芯片合成_初始化", ["芯片合成_跳过"])]
    assert ok_ready is True and ready_status == "ready"


def _patch_synth_once_primitives(slots_empty, cost_text="消耗道具  210"):
    """把 _synth_once 依赖的界面原语替换成可控结果，返回恢复函数。

    cost_text=None 模拟「消耗道具」那行没读出来（只剩确定/取消）。
    """
    originals = {
        name: getattr(chip_synth, name)
        for name in ("_click_text", "_shot", "_ocr", "_filled_count", "_dismiss_output")
    }
    original_sleep = chip_synth.time.sleep
    chip_synth._click_text = lambda *args, **kwargs: True
    chip_synth._shot = lambda _context: "frame"
    texts = [("确定", (0, 0, 1, 1)), ("取消", (0, 0, 1, 1))]
    if cost_text is not None:
        texts.append((cost_text, (0, 0, 1, 1)))
    chip_synth._ocr = lambda _context, _image, _roi: list(texts)
    chip_synth._filled_count = lambda _image: 0 if slots_empty else 3
    chip_synth._dismiss_output = lambda _context: True
    chip_synth.time.sleep = lambda _seconds: None

    def restore():
        for name, value in originals.items():
            setattr(chip_synth, name, value)
        chip_synth.time.sleep = original_sleep

    return restore


def _picked_card():
    return {"row": 0, "col": 0, "detail": _detail(2, (1, 1, 1)),
            "detail_text": "连击2 [攻击1/命中1/暴伤1]"}


def _synth_once(currency_after, slots_empty, cost_text="消耗道具  210"):
    action = chip_synth.ChipSynthAction()
    session = SimpleNamespace(dir=Path("sandbox/test"), report={},
                              shot=lambda _context, _name: None)
    action._currency = lambda _context: currency_after
    restore = _patch_synth_once_primitives(slots_empty, cost_text)
    try:
        return action._synth_once(SimpleNamespace(), session, {"skill": "连击"}, 19061,
                                  [_picked_card()])
    finally:
        restore()


def test_synth_once_committed_when_currency_drops_by_cost():
    output = _synth_once(18851, True)  # 正好少 210
    assert output["state"] == "synth_committed"
    assert output["cost"] == 210 and output["delta"] == 210
    assert output["materials"] == [{"row": 1, "col": 1, "levels": [2, 1, 1, 1],
                                    "detail": "连击2 [攻击1/命中1/暴伤1]"}]
    # 产出那张不读属性：返回里不带 detail / levels
    assert "detail" not in output and "levels" not in output


def test_synth_once_cost_source_is_ocr_when_read():
    output = _synth_once(18851, True)
    assert output["cost_source"] == "ocr" and output["cost"] == 210


def test_synth_once_cost_unconfirmed_when_cost_not_read():
    # 钱扣了、槽也空了，但「消耗道具」没读出来 → 本轮判成本未确认，不静默按 210 记账
    output = _synth_once(18851, True, cost_text=None)
    assert output["state"] == "cost_unconfirmed"
    assert output["cost_source"] == "fallback" and output["cost"] == 210
    assert output["delta"] == 210


def test_synth_once_failed_when_currency_unchanged():
    output = _synth_once(19061, True)  # 没扣钱
    assert output["state"] == "failed"
    assert output["delta"] == 0 and output["cost"] == 210


def test_synth_once_failed_when_delta_is_not_exact_cost():
    output = _synth_once(18961, True)  # 只少 100，不是「正好等于消耗道具」
    assert output["state"] == "failed"
    assert output["delta"] == 100


def test_synth_once_failed_when_slots_not_empty():
    output = _synth_once(18851, False)  # 钱扣了但材料槽没清空
    assert output["state"] == "failed"
    assert output["delta"] == 210 and output["slots_empty"] is False


class _ProcessSession:
    def __init__(self):
        self.shots = []
        self.dry_run = False
        self.max_rounds = 1
        self.options = {"threshold": [2, 1, 1, 1], "threshold_raw": "2/1/1/1"}
        self.report = {"currency_before": 19061, "currency_after": None,
                       "produced": [], "skills": [], "truncated": False}

    def shot(self, _context, name):
        self.shots.append(name)
        return name

    def flush(self):
        pass


def _process_entry():
    return {"skill": "连击", "scanned": 0, "eligible": 0, "locked_skipped": 0,
            "rounds": 0, "consumed_chips": 0, "consumed_currency": 0,
            "outputs": [], "status": "pending", "notes": [], "cards": []}


def _cards(count):
    return [
        {"point": (254 + (i % 3) * 122, 228 + (i // 3) * 122), "row": i // 3, "col": i % 3,
         "detail": _detail(2, (1, 1, 1)), "detail_text": "连击2 [攻击1/命中1/暴伤1]"}
        for i in range(count)
    ]


def test_process_skill_stops_when_currency_mismatch():
    action = chip_synth.ChipSynthAction()
    session = _ProcessSession()
    entry = _process_entry()
    action._clear_slots = lambda *args, **kwargs: True
    action._apply_filter = lambda *args, **kwargs: True
    action._list_empty = lambda *args, **kwargs: False
    action._scan = lambda *args, **kwargs: _cards(3)
    action._select_three = lambda *args, **kwargs: _cards(3)
    action._currency = lambda *args, **kwargs: 19061
    action._synth_once = lambda *args, **kwargs: {
        "state": "failed", "cost": 210, "delta": 0, "slots_empty": True, "materials": []}
    context = SimpleNamespace(tasker=SimpleNamespace(running=True))
    try:
        ok = action._process_skill(context, session, "连击", entry)
    finally:
        for name in ("_clear_slots", "_apply_filter", "_list_empty", "_scan",
                     "_select_three", "_currency", "_synth_once"):
            delattr(action, name)

    assert ok is True
    assert entry["status"] == "synth_failed"
    assert entry["rounds"] == 0 and entry["consumed_currency"] == 0
    assert any("交易未确认" in note for note in entry["notes"])


def test_process_skill_stops_when_cost_unconfirmed():
    action = chip_synth.ChipSynthAction()
    session = _ProcessSession()
    session.max_rounds = 3
    entry = _process_entry()
    action._clear_slots = lambda *args, **kwargs: True
    action._apply_filter = lambda *args, **kwargs: True
    action._list_empty = lambda *args, **kwargs: False
    action._scan = lambda *args, **kwargs: _cards(3)
    action._select_three = lambda *args, **kwargs: _cards(3)
    action._currency = lambda *args, **kwargs: 19061
    action._synth_once = lambda *args, **kwargs: {
        "state": "cost_unconfirmed", "cost": 210, "cost_source": "fallback",
        "delta": 210, "slots_empty": True, "materials": []}
    context = SimpleNamespace(tasker=SimpleNamespace(running=True))
    try:
        ok = action._process_skill(context, session, "连击", entry)
    finally:
        for name in ("_clear_slots", "_apply_filter", "_list_empty", "_scan",
                     "_select_three", "_currency", "_synth_once"):
            delattr(action, name)

    assert ok is True
    assert entry["status"] == "cost_unconfirmed"
    assert entry["stop_reason"] == "cost_unconfirmed"
    assert entry["rounds"] == 0 and entry["consumed_currency"] == 0
    assert entry["cost_sources"] == ["fallback"]
    assert any("成本未确认" in note for note in entry["notes"])


def test_process_skill_keeps_going_while_currency_drops():
    action = chip_synth.ChipSynthAction()
    session = _ProcessSession()
    session.max_rounds = None
    entry = _process_entry()
    scans = [_cards(6), _cards(3), _cards(2)]
    action._clear_slots = lambda *args, **kwargs: True
    action._apply_filter = lambda *args, **kwargs: True
    action._list_empty = lambda *args, **kwargs: False
    def fake_scan(_context, _session, _skill, _entry):
        found = scans.pop(0)
        _entry["cards"] = list(found)
        return list(found)

    action._scan = fake_scan
    action._select_three = lambda *args, **kwargs: _cards(3)
    action._currency = lambda *args, **kwargs: 19061 - 210 * entry["rounds"]
    action._synth_once = lambda *args, **kwargs: {
        "state": "synth_committed", "cost": 210, "delta": 210, "slots_empty": True,
        "materials": []}
    context = SimpleNamespace(tasker=SimpleNamespace(running=True))
    try:
        ok = action._process_skill(context, session, "连击", entry)
    finally:
        for name in ("_clear_slots", "_apply_filter", "_list_empty", "_scan",
                     "_select_three", "_currency", "_synth_once"):
            delattr(action, name)

    assert ok is True
    assert entry["rounds"] == 2 and entry["consumed_currency"] == 420
    assert entry["states"] == ["synth_committed", "synth_committed"]
    assert entry["status"] == "done"
    assert entry["remaining_cards"] == _cards(2)
    assert entry["stop_reason"] == "stock_short"


def test_process_skill_stops_at_max_rounds():
    """第一行每次都满 3 张命中 → 一直合到 max_rounds 才停。"""
    action = chip_synth.ChipSynthAction()
    session = _ProcessSession()
    session.max_rounds = 3
    entry = _process_entry()
    action._clear_slots = lambda *args, **kwargs: True
    action._apply_filter = lambda *args, **kwargs: True
    action._list_empty = lambda *args, **kwargs: False
    action._scan = lambda _context, _session, _skill, _entry: _cards(3)
    action._select_three = lambda *args, **kwargs: _cards(3)
    action._currency = lambda *args, **kwargs: 19061 - 210 * entry["rounds"]
    action._synth_once = lambda *args, **kwargs: {
        "state": "synth_committed", "cost": 210, "delta": 210, "slots_empty": True,
        "materials": []}
    context = SimpleNamespace(tasker=SimpleNamespace(running=True))
    try:
        ok = action._process_skill(context, session, "连击", entry)
    finally:
        for name in ("_clear_slots", "_apply_filter", "_list_empty", "_scan",
                     "_select_three", "_currency", "_synth_once"):
            delattr(action, name)

    assert ok is True
    assert entry["rounds"] == 3 and entry["consumed_currency"] == 630
    assert entry["stop_reason"] == "max_rounds"
    assert any("max_rounds" in note for note in entry["notes"])


def test_unknown_detail_blocks_synthesis():
    """有明细未知的卡时判定不完整，不许往合成推进。"""
    action = chip_synth.ChipSynthAction()
    session = _ProcessSession()
    entry = {"skill": "连击", "scanned": 12, "eligible": 3, "locked_skipped": 0,
             "rounds": 0, "consumed_chips": 0, "consumed_currency": 0,
             "outputs": [], "status": "pending", "notes": [], "cards": [],
             "unknown": 2, "unknown_cards": [{"row": 1, "col": 1, "reason": "详情打不开"}],
             "scan_truncated": False}
    cards = [
        {"point": (254, 228), "row": 0, "col": 0, "detail": _detail(2, (1, 1, 1)),
         "detail_text": "连击2 [攻击1/命中1/暴伤1]"},
        {"point": (376, 228), "row": 0, "col": 1, "detail": _detail(2, (1, 1, 1)),
         "detail_text": "连击2 [攻击1/命中1/暴伤1]"},
        {"point": (498, 228), "row": 0, "col": 2, "detail": _detail(2, (1, 1, 1)),
         "detail_text": "连击2 [攻击1/命中1/暴伤1]"},
    ]
    synth_calls = []
    action._clear_slots = lambda *args, **kwargs: True
    action._apply_filter = lambda *args, **kwargs: True
    action._list_empty = lambda *args, **kwargs: False
    action._scan = lambda *args, **kwargs: list(cards)
    action._select_three = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不许选卡"))
    action._synth_once = lambda *args, **kwargs: synth_calls.append(1)
    context = SimpleNamespace(tasker=SimpleNamespace(running=True))
    try:
        ok = action._process_skill(context, session, "连击", entry)
    finally:
        for name in ("_clear_slots", "_apply_filter", "_list_empty", "_scan",
                     "_select_three", "_synth_once"):
            delattr(action, name)

    assert ok is True
    assert entry["status"] == "incomplete"
    assert synth_calls == [] and entry["rounds"] == 0
    assert any("明细未知" in note for note in entry["notes"])


def _patch_scan_primitives(open_ok, cells_move, present=None):
    originals = {
        name: getattr(chip_synth, name)
        for name in ("_shot", "_card_present", "_filled_count", "_open_detail",
                     "_close_detail", "_swipe", "_cell_diff", "_crop_grid")
    }
    present = {(254, 228)} if present is None else present
    chip_synth._shot = lambda _context: "frame"
    chip_synth._card_present = lambda _image, point: point in present
    chip_synth._filled_count = lambda _image: 0
    chip_synth._open_detail = lambda _context, _point, _evidence=None: open_ok
    chip_synth._close_detail = lambda _context: True
    chip_synth._swipe = lambda *args, **kwargs: None
    chip_synth._cell_diff = lambda before, after: cells_move
    chip_synth._crop_grid = lambda image: image

    def restore():
        for name, value in originals.items():
            setattr(chip_synth, name, value)

    return restore


def test_scan_marks_unknown_when_detail_will_not_open():
    action = chip_synth.ChipSynthAction()
    session = SimpleNamespace(report={}, options={"threshold": [2, 1, 1, 1],
                                                 "threshold_raw": "2/1/1/1"})
    entry = {"skill": "连击", "scanned": 0, "locked_skipped": 0, "notes": [], "cards": []}
    restore = _patch_scan_primitives(open_ok=False, cells_move=0.0)
    try:
        candidates = action._scan(SimpleNamespace(), session, "连击", entry)
    finally:
        restore()

    assert candidates == [] and entry["cards"] == []
    assert entry["unknown"] == 1
    assert entry["unknown_cards"][0]["reason"].startswith("详情打不开")
    assert any("明细未知" in note for note in entry["notes"])


def test_scan_only_reads_first_row_three_cells():
    action = chip_synth.ChipSynthAction()
    session = SimpleNamespace(report={}, options={"threshold": [2, 1, 1, 1],
                                                 "threshold_raw": "2/1/1/1"})
    entry = {"skill": "连击", "scanned": 0, "locked_skipped": 0, "notes": [], "cards": []}
    present = {(x, chip_synth.GRID_ROWS[0]) for x in chip_synth.GRID_COLS}
    restore = _patch_scan_primitives(open_ok=True, cells_move=9.9, present=present)
    action._read_detail = lambda _context, _evidence=None: _detail(1, (5, 5, 5))
    try:
        candidates = action._scan(SimpleNamespace(), session, "连击", entry)
    finally:
        restore()
        del action._read_detail

    assert len(candidates) == 3
    assert entry["scanned"] == 3 and entry["scanned_scope"] == "first_row_only"
    assert session.report["scanned_scope"] == "first_row_only"
    assert session.report.get("scan_truncated") is not True
    assert not any("本轮结束" in note for note in entry["notes"])


def test_scan_stops_when_first_row_not_all_hit():
    action = chip_synth.ChipSynthAction()
    session = SimpleNamespace(report={}, options={"threshold": [2, 1, 1, 1],
                                                 "threshold_raw": "2/1/1/1"})
    entry = {"skill": "连击", "scanned": 0, "locked_skipped": 0, "notes": [], "cards": []}
    present = {(x, chip_synth.GRID_ROWS[0]) for x in chip_synth.GRID_COLS}
    details = iter([_detail(1, (5, 5, 5)), _detail(1, (5, 5, 5)), _detail(3, (0, 0, 0))])
    restore = _patch_scan_primitives(open_ok=True, cells_move=9.9, present=present)
    action._read_detail = lambda _context, _evidence=None: next(details)
    try:
        candidates = action._scan(SimpleNamespace(), session, "连击", entry)
    finally:
        restore()
        del action._read_detail

    assert len(candidates) == 2  # 第三张没命中 → 本轮结束，不往下翻找
    assert entry["scanned"] == 3
    assert any("不往下翻找" in note for note in entry["notes"])


def _four_row_groups():
    """四行分组：主技能行 OCR 只回「等级。」（真机稳定态就是这样），副技能三行正常。"""
    return [
        {"y": 236.0, "items": [("暴怒", [536, 224, 40, 26]), ("等级。", [704, 225, 48, 24])]},
        {"y": 289.0, "items": [("命中", [536, 278, 38, 21]), ("等级.1", [705, 278, 90, 20])]},
        {"y": 337.0, "items": [("坚韧", [536, 327, 38, 22]), ("等级.1", [705, 328, 89, 19])]},
        {"y": 386.0, "items": [("攻击", [536, 376, 37, 23]), ("等级.2", [705, 377, 66, 21])]},
    ]


def test_main_level_comes_from_template_not_ocr():
    groups = _four_row_groups()
    # 旧路径：主技能行 OCR 没有数字 → 整张卡判未知
    assert chip_synth._detail_from_groups(groups) is None
    # 新路径：主技能等级由模板给出，OCR 只负责技能名与副技能三行
    detail = chip_synth._detail_from_groups(groups, 3)
    assert detail["main_skill"] == {"name": "暴怒", "level": 3}
    assert [sub["name"] for sub in detail["sub_skills"]] == ["命中", "坚韧", "攻击"]
    assert [sub["level"] for sub in detail["sub_skills"]] == [1, 1, 2]
    # 模板读不出等级时不能退化成 1 级
    assert chip_synth._detail_from_groups(groups, None) is None


def test_main_level_below_threshold_is_unknown():
    blank = np.full((720, 1280, 3), 128, dtype=np.uint8)
    level, scores = chip_synth._match_main_level(blank)
    assert level is None
    assert max(scores.values()) < chip_synth.MAIN_LEVEL_THRESHOLD


def _pasted_level(level):
    """把某一级模板贴回主技能行的实际坐标，造一张「只含该等级」的最小画布。"""
    tpl = chip_synth._level_templates()[level]
    canvas = np.full((720, 1280, 3), 88, dtype=np.uint8)  # 主技能行深棕底
    canvas[229:229 + tpl.shape[0], 758:758 + tpl.shape[1], :] = tpl[..., None].astype(np.uint8)
    return canvas


def test_main_level_templates_recognize_themselves():
    templates = chip_synth._level_templates()
    assert sorted(templates) == [1, 2, 3]
    for level, tpl in templates.items():
        canvas = np.full((720, 1280, 3), 88, dtype=np.uint8)  # 主技能行深棕底
        x, y, = 758, 229
        canvas[y:y + tpl.shape[0], x:x + tpl.shape[1], :] = tpl[..., None].astype(np.uint8)
        got, scores = chip_synth._match_main_level(canvas)
        assert got == level, (level, scores)
        assert scores[level] >= chip_synth.MAIN_LEVEL_THRESHOLD


def test_main_level_width_matches_each_grade():
    """三种等级各自的宽度判据：1 级窄（<=6），2/3 级宽（>=7）。"""
    widths = {}
    x, y, w, h = chip_synth.MAIN_LEVEL_ROI
    for level in (1, 2, 3):
        frame = _pasted_level(level)
        gray = chip_synth._gray(frame)
        _, at = chip_synth._ncc_best(gray[y:y + h, x:x + w], chip_synth._level_templates()[level])
        top, left = y + at[1], x + at[0]
        widths[level] = chip_synth._digit_width(gray, top, left)
    assert widths[1] <= chip_synth.MAIN_LEVEL_WIDTH_SPLIT
    assert widths[2] > chip_synth.MAIN_LEVEL_WIDTH_SPLIT
    assert widths[3] > chip_synth.MAIN_LEVEL_WIDTH_SPLIT


def test_main_level_threshold_boundary():
    """阈值 0.85 的边界：0.86 通过，0.849/0.84 判 unknown（宽度须与模板一致）。"""
    assert chip_synth.MAIN_LEVEL_THRESHOLD == 0.85
    original = chip_synth._ncc_best
    lv2 = chip_synth._level_templates()[2]
    try:
        for value, expect in ((0.86, 2), (0.849, None), (0.84, None)):
            def fake(patch, template, v=value):
                score = v if template is lv2 else max(0.0, v - 0.3)
                return score, (4, 15)  # 命中位置对齐画布里 lv2 模板的粘贴点 (x=758, y=229)
            chip_synth._ncc_best = fake
            level, scores = chip_synth._match_main_level(_pasted_level(2))
            assert level == expect, (value, level, scores)
    finally:
        chip_synth._ncc_best = original


def test_main_level_width_conflict_is_unknown():
    """宽度与模板结论不一致 → unknown，绝不按模板硬报等级。"""
    original = chip_synth._digit_width
    try:
        # 模板说 1 级，宽度却说「宽」（>=7）→ 冲突
        chip_synth._digit_width = lambda *args: 8
        level, scores = chip_synth._match_main_level(_pasted_level(1))
        assert level is None and scores[1] >= chip_synth.MAIN_LEVEL_THRESHOLD
        # 模板说 2/3 级，宽度却说「窄」（<=6）→ 冲突
        chip_synth._digit_width = lambda *args: 4
        for grade in (2, 3):
            level, scores = chip_synth._match_main_level(_pasted_level(grade))
            assert level is None, (grade, scores)
    finally:
        chip_synth._digit_width = original


def test_main_level_width_unreadable_is_unknown():
    """宽度读不出或超出生理范围（<3 或 >10）→ unknown。"""
    original = chip_synth._digit_width
    try:
        for width in (None, 2, 11):
            chip_synth._digit_width = lambda *args, w=width: w
            level, _ = chip_synth._match_main_level(_pasted_level(2))
            assert level is None, width
    finally:
        chip_synth._digit_width = original


def test_init_mode_panel_first_env_override():
    action = chip_synth.ChipSynthAction()
    context = SimpleNamespace(override_next=lambda *args: None,
                              tasker=SimpleNamespace(running=True))
    original_config = chip_synth._config_options
    original_synth = chip_synth._Synth
    created = []

    class _CaptureSession:
        def __init__(self, options, dry_run):
            self.options = options
            self.dry_run = dry_run
            self.dir = Path("sandbox/test")
            self.report = {"status": "running"}
            self.max_rounds = None
            created.append(self)

        def flush(self):
            pass

    chip_synth._config_options = lambda: {"skills": ["连击"], "threshold": "2/1/1/1"}
    chip_synth._Synth = _CaptureSession
    env_names = ("LAA_CHIP_SYNTH_REAL", "LAA_CHIP_SYNTH_MAX_ROUNDS")
    saved = {name: os.environ.get(name) for name in env_names}
    try:
        for name in env_names:
            os.environ.pop(name, None)
        # 面板默认「真跑」：参数里没有 dry_run 也按真跑
        action._init(context, None, {"max_rounds": 5})
        assert created[-1].dry_run is False and created[-1].max_rounds == 5
        assert created[-1].report["mode"] == "REAL"
        assert created[-1].report["mode_source"] == "panel:芯片合成_执行模式"
        # 面板选「干跑」：dry_run=true 生效
        action._init(context, None, {"dry_run": True})
        assert created[-1].dry_run is True
        assert created[-1].report["mode"] == "DRY RUN"
        # 环境变量只作调试覆盖，优先级高于面板
        os.environ["LAA_CHIP_SYNTH_REAL"] = "1"
        os.environ["LAA_CHIP_SYNTH_MAX_ROUNDS"] = "1"
        action._init(context, None, {"dry_run": True, "max_rounds": 5})
        assert created[-1].dry_run is False and created[-1].max_rounds == 1
        assert created[-1].report["mode_source"] == "env:LAA_CHIP_SYNTH_REAL"
        os.environ["LAA_CHIP_SYNTH_REAL"] = "0"
        action._init(context, None, {})
        assert created[-1].dry_run is True
        assert created[-1].report["mode_source"] == "env:LAA_CHIP_SYNTH_REAL"
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        chip_synth._config_options = original_config
        chip_synth._Synth = original_synth


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print("CHIP_SYNTH_OK (%d tests)" % len(tests))
