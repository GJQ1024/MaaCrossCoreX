# -*- coding: utf-8 -*-
"""基建换班 —— 触发=红脸，动作=整组切「没在工作那套」预设。

分工（用户定死的规则）：
  * 生产设施（有红/绿心情）：整组切预设，不许拆编队、不许挑人补位。
  * 宿舍（最后一行、没有预设）：人工换人，一次一行；本文件只留禁用骨架，
    绝不能被日常链路调到（节点 基建_换班_人工干预 是 enabled=false）。

日常链路唯一入口：infra_shift → 节点 基建_换班。
四层复核，任一不过就存图并失败退出（交给 on_error 停止任务）：
  ① 重开预设弹窗：目标按钮 使用→工作中，原工作按钮 →使用；
  ② 面板外电力/负载文案必须变化（实测同功耗，只记录）；
  ③ 切完等 2.5s，该设施整组没有红脸；
     例外：单槽设施（合成工厂）只有 1 名驻员，其心情被一次合成操作瞬时消耗，
     红脸属于正常工作态而不是换班失败，因此不做「红比例归零」硬校验，
     只记录 WARN 并继续下一个设施；红槽数意外上升仍判失败；
  ④ 误伤：其他设施可见槽位数/红绿态、全局人数不变。

黑名单：预设3/4 的 ⚠50 永不点击；任何购买/消耗按钮不点。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from maa.custom_action import CustomAction

import infra_mood as mood
import workspace_paths
from job_next import write_hint


def _report_next_run(swapped: bool) -> None:
    """任务自报 NextRun：面板只有红/绿徽章、没有倒计时，因此不写 notBefore；
    只标记本次是否真换班，时间由壳按实测「换班→红脸」间隔学习、样本不足时用静态 480 分钟。"""
    write_hint("infra_shift", minutes=None, source="self", swapped=swapped)

_MCC_ROOT = Path(__file__).resolve().parents[1]
_LOG_FILE = _MCC_ROOT / "logs" / "infra_shift.log"
_BASELINE_DIR = os.environ.get("INFRA_RED_BASELINE_DIR") or str(
    workspace_paths.maa_root(_MCC_ROOT) / "sandbox" / "red_baseline"
)
_OPTION_NAME = "基建_换班预设覆盖"
# 单槽设施：整栋只有 1 名驻员（合成工厂），且该角色心情由合成操作瞬时消耗。
# 红脸不代表换班没做成，复核③对它们降级为记录，不作失败。
SINGLE_SLOT_FACILITIES = ("合成工厂",)

log = logging.getLogger("laa.infra")

_POWER_ROI_REF = [980, 15, 300, 100]
_MODAL_ROI_REF = [200, 120, 880, 500]
_PRESET_BTN_DX_REF = 672
_PRESET_BTN_DY_REF = 4
_MODAL_BADGE_DX_REF = (23, 146, 267)
_MODAL_BADGE_DY_REF = 125
# 5 槽设施的弹窗头像列（实测 03:21 挖掘矿场弹窗：相对「预设N」标题框左边 +24/+160/+288/+412/+546）
_MODAL_BADGE_DX_5_REF = (24, 160, 288, 412, 546)
_MODAL_BADGE_DY_5_REF = 128


# ---------------------------------------------------------------- 基础设施

def _note(msg: str):
    line = "[infra] " + msg
    log.warning(line)
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as fp:
            fp.write("%s %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _shot(context):
    return context.tasker.controller.post_screencap().wait().get()


def _sleep(seconds: float):
    time.sleep(seconds)


def _click(context, x, y):
    context.tasker.controller.post_click(int(x), int(y)).wait()


def _swipe(context, x1, y1, x2, y2, duration=350):
    context.tasker.controller.post_swipe(int(x1), int(y1), int(x2), int(y2), duration).wait()


def _back(context):
    context.tasker.controller.post_press_key(4).wait()
    _sleep(1.0)


def _scale(image):
    h, w = image.shape[:2]
    return w / float(mood.REF_W), h / float(mood.REF_H)


def _scaled_roi(roi_ref, image):
    sx, sy = _scale(image)
    return [int(round(roi_ref[0] * sx)), int(round(roi_ref[1] * sy)),
            int(round(roi_ref[2] * sx)), int(round(roi_ref[3] * sy))]


def _save(image, tag):
    try:
        from PIL import Image
        folder = _BASELINE_DIR
        Path(folder).mkdir(parents=True, exist_ok=True)
        path = Path(folder) / ("%s_%s.png" % (datetime.now().strftime("%Y%m%d_%H%M%S"), tag))
        Image.fromarray(mood.image_rgb(image).astype("uint8")).save(path)
        _note("截图 %s" % path)
        return str(path)
    except Exception as exc:  # pragma: no cover
        _note("截图保存失败: %s" % exc)
        return None


def _ocr_all(context, image, roi, node="基建_驻员状况", threshold=0.2):
    detail = context.run_recognition(
        node, image,
        pipeline_override={node: {
            "recognition": "OCR",
            "roi": roi,
            "expected": [],
            "threshold": threshold,
        }},
    )
    if detail is None:
        return []
    out = []
    for item in getattr(detail, "all_results", []) or []:
        text = "".join(str(getattr(item, "text", "") or "").split())
        box = list(getattr(item, "box", []) or [])
        if len(box) < 4:
            continue
        out.append((text, [int(v) for v in box[:4]], float(getattr(item, "score", 0) or 0)))
    return out


def _globals(context, image):
    items = _ocr_all(context, image, _scaled_roi(_POWER_ROI_REF, image))
    power = load = staff = None
    for text, _box, _score in items:
        m = re.search(r"(\d{2,5})/(\d{2,5})k?w", text, re.I)
        if m and power is None:
            power = "%s/%s" % (m.group(1), m.group(2))
        if ("负载" in text or "充裕" in text) and load is None:
            load = text
        m2 = re.search(r"(\d+)/(\d+)p", text, re.I)
        if m2 and staff is None:
            staff = "%s/%s" % (m2.group(1), m2.group(2))
    return {"power": power, "load": load, "staff": staff}


def _scroll_down(context):
    sx, sy = 1.0, 1.0
    _swipe(context, 640, 600, 640, 280, 350)
    _sleep(0.9)


def _scroll_up_one(context):
    _swipe(context, 640, 280, 640, 600, 350)
    _sleep(0.9)


def _scroll_to_top(context, empty_retry=3):
    """实测一次 swipe 只能滚约一行，必须以「看到行星指挥部」为到顶标志。

    OCR 偶发一次读空（面板动画/截图抖动），不能把「这一次读空」当到顶；
    连续 empty_retry 次都读不到才放弃。
    """
    for _ in range(8):
        image = _shot(context)
        boxes = mood.ocr_name_boxes(context, image)
        if "行星指挥部" in boxes:
            return image
        if not boxes:
            for _retry in range(empty_retry):
                _sleep(0.5)
                image = _shot(context)
                boxes = mood.ocr_name_boxes(context, image)
                if boxes:
                    break
            if not boxes:
                return image  # 连续读空：面板不在/不在基建页
        _scroll_up_one(context)
    return _shot(context)


def _modal_scroll(context):
    _swipe(context, 640, 520, 640, 330, 400)
    _sleep(0.8)


# ---------------------------------------------------------------- 面板测量

def _measure_view(context, image):
    rgb = mood.image_rgb(image)
    boxes = mood.ocr_name_boxes(context, image)
    return {name: mood.measure_row(rgb, name, box) for name, box in boxes.items()}, boxes


def _scan_once(context):
    rows = {}
    globs = None
    _scroll_to_top(context)
    last_names = None
    for _view in range(5):
        image = _shot(context)
        if globs is None:
            globs = _globals(context, image)
        boxes = mood.ocr_name_boxes(context, image)
        rgb = mood.image_rgb(image)
        for name, box in boxes.items():
            if name not in rows:
                rows[name] = mood.measure_row(rgb, name, box)
        if len(rows) >= len(mood.FACILITY_NAMES):
            break
        if last_names is not None and set(boxes) == last_names:
            break  # 已滚到底，再滚也看不到新行
        last_names = set(boxes)
        _scroll_down(context)
    _scroll_to_top(context)
    return rows, globs


def _scan_panel(context, attempts=3):
    """整轮扫描重试：OCR 一次读空不代表面板不在，别一次就判失败。"""
    rows, globs = {}, None
    for attempt in range(attempts):
        rows, globs = _scan_once(context)
        if rows:
            return rows, globs
        _note("第 %d/%d 次扫描没认出设施行，0.8s 后重试" % (attempt + 1, attempts))
        _sleep(0.8)
    return rows, globs


def _dump_baseline(rows, globs):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "power": globs.get("power"),
        "load": globs.get("load"),
        "staff": globs.get("staff"),
        "rows": rows,
        "red": [
            {
                "facility": row["facility"],
                "slot": slot["index"],
                "rgb": slot["mean_rgb"],
                "red_ratio": slot["red"],
                "green_ratio": slot["green"],
            }
            for row in rows.values()
            for slot in row.get("slots", [])
            if slot["red"] >= mood.SLOT_RED_MIN
        ],
    }
    Path(_BASELINE_DIR).mkdir(parents=True, exist_ok=True)
    path = Path(_BASELINE_DIR) / ("red_baseline_%s.json" % stamp)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _note("红脸基线 %s" % path)
    return path, data


# ---------------------------------------------------------------- 预设弹窗

def _parse_presets(context, image):
    items = _ocr_all(context, image, _scaled_roi(_MODAL_ROI_REF, image))
    headers = []
    buttons = []
    locked = []
    for text, box, score in items:
        m = re.search(r"预设(\d+)", text)
        if m:
            headers.append((box[1], int(m.group(1)), box, score))
            continue
        if text in ("使用", "工作中"):
            center = (box[0] + box[2] // 2, box[1] + box[3] // 2)
            buttons.append((text, center, box, score))
            continue
        if "50" in text or "△" in text or "⚠" in text:
            locked.append((box[1], box, score))
    rows = {}
    for hy, number, hbox, _score in headers:
        if number not in rows:
            rows[number] = {"header": hbox, "button": None, "center": None, "locked": False}
    for text, center, box, score in buttons:
        best = None
        for hy, number, hbox, _score in headers:
            if hy <= center[1] and center[1] - hy <= 160:
                if best is None or hy > best[0]:
                    best = (hy, number)
        if best is None:
            continue
        row = rows.setdefault(best[1], {"header": None, "button": None,
                                        "center": None, "locked": False})
        if row["button"] is None or score > 0.0:
            row["button"] = text
            row["center"] = center
            row["box"] = box
    for by, box, _score in locked:
        best = None
        for hy, number, hbox, _score2 in headers:
            if hy <= by and by - hy <= 200:
                if best is None or hy > best[0]:
                    best = (hy, number)
        if best is not None:
            row = rows.setdefault(best[1], {"header": None, "button": None,
                                            "center": None, "locked": False})
            row["locked"] = True
    return rows


def _collect_presets(context):
    merged = {}
    for _ in range(3):
        image = _shot(context)
        parsed = _parse_presets(context, image)
        for number, row in parsed.items():
            merged.setdefault(number, row)
        if any(row.get("button") == "使用" for row in merged.values()):
            break
        _modal_scroll(context)
    return merged


def _modal_row_slots(image, header_box, slot_count):
    """读弹窗里某套预设的头像徽章；3 槽 / 5 槽各一套实测偏移。"""
    if not header_box:
        return None
    if slot_count == 3:
        dxs, dy = _MODAL_BADGE_DX_REF, _MODAL_BADGE_DY_REF
    elif slot_count == 5:
        dxs, dy = _MODAL_BADGE_DX_5_REF, _MODAL_BADGE_DY_5_REF
    else:
        return None
    rgb = mood.image_rgb(image)
    sx = image.shape[1] / float(mood.REF_W)
    sy = image.shape[0] / float(mood.REF_H)
    out = []
    for dx in dxs:
        center = (int(round(header_box[0] + dx * sx)),
                  int(round(header_box[1] + dy * sy)))
        slot = mood.measure_slot(rgb, center, sx)
        if slot is None:
            return None
        out.append(slot)
    return out


def _modal_row_red(context, image, header_box, slot_count):
    """返回该预设里最红的一枚徽章的红占比；读不到返回 None。"""
    slots = _modal_row_slots(image, header_box, slot_count)
    return None if slots is None else max(slot["red"] for slot in slots)


def _click_preset_button(context, name_box, image):
    sx, sy = _scale(image)
    x = name_box[0] + _PRESET_BTN_DX_REF * sx
    y = name_box[1] + _PRESET_BTN_DY_REF * sy
    _click(context, x, y)


# ---------------------------------------------------------------- 配置

def _instance_config_path():
    for key in ("MFA_INSTANCE_CONFIG_PATH", "MAA_INSTANCE_CONFIG_PATH", "MFA_INSTANCE_CONFIG"):
        value = os.environ.get(key)
        if value and Path(value).is_file():
            return Path(value)
    return None


def _preferred_presets():
    """interface.json 选项「基建_换班预设覆盖」，值形如 {"行星指挥部": 1}。"""
    path = _instance_config_path()
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    for task in data.get("TaskItems") or data.get("task") or []:
        if not isinstance(task, dict):
            continue
        if task.get("entry") not in ("基建", "基建_主体"):
            continue
        for opt in task.get("option") or []:
            if not isinstance(opt, dict) or opt.get("name") != _OPTION_NAME:
                continue
            raw = opt.get("value", opt.get("values", opt.get("input")))
            if isinstance(raw, dict) and "value" in raw:
                raw = raw["value"]
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("value"):
                        raw = item["value"]
                        break
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    return {}
            if isinstance(raw, dict):
                out = {}
                for name, number in raw.items():
                    try:
                        out[str(name)] = int(number)
                    except Exception:
                        continue
                return out
    return {}


# ---------------------------------------------------------------- 复核

def _locate_row(context, name, max_scroll=4):
    for _ in range(max_scroll + 1):
        image = _shot(context)
        boxes = mood.ocr_name_boxes(context, image)
        box = boxes.get(name)
        sy = image.shape[0] / float(mood.REF_H)
        if box and 150 * sy <= box[1] <= 560 * sy:
            return box, image
        _scroll_down(context) if (box and box[1] > 560 * sy) or not box else _scroll_up_one(context)
    return None, None


def _mischief(baseline, current_rows, target, globs_before, globs_after, handled=()):
    problems = []
    for name, before in baseline.items():
        if name == target or name in handled or before.get("state") == "unmeasured":
            continue
        after = current_rows.get(name)
        if after is None:
            continue
        if after.get("state") != before.get("state"):
            problems.append("%s 心情态 %s→%s" % (name, before.get("state"), after.get("state")))
        if len(after.get("slots", [])) != len(before.get("slots", [])):
            problems.append("%s 可见槽位 %d→%d" % (
                name, len(before.get("slots", [])), len(after.get("slots", []))))
    if globs_before.get("staff") and globs_after.get("staff") \
            and globs_before["staff"] != globs_after["staff"]:
        problems.append("全局人数 %s→%s" % (globs_before["staff"], globs_after["staff"]))
    return problems


# ---------------------------------------------------------------- 主流程

def _process_facility(context, name, baseline, preferred, handled=()):
    sx_note = ""
    box, image = _locate_row(context, name)
    if not box:
        _save(_shot(context), "fail_locate_%s" % name)
        _note("%s：切换前定位失败" % name)
        return False
    before_globals = _globals(context, image)
    before_row = mood.measure_row(mood.image_rgb(image), name, box)
    if not mood.needs_shift(before_row):
        _note("%s：已无红脸，幂等跳过" % name)
        return True
    _note("%s：检测到红脸槽位 %s" % (name, before_row["red_slots"]))

    _click_preset_button(context, box, image)
    _sleep(1.5)
    presets = _collect_presets(context)
    if not presets:
        _save(_shot(context), "fail_modal_%s" % name)
        _note("%s：预设弹窗没读到任何预设" % name)
        return False

    working = [n for n, row in presets.items() if row.get("button") == "工作中"]
    candidates = [n for n, row in presets.items()
                  if row.get("button") == "使用" and not row.get("locked")]

    if name in preferred:
        target = preferred[name]
        if target in presets and presets[target].get("button") == "工作中":
            _note("%s：目标预设%s 已在工作中，幂等跳过" % (name, target))
            _back(context)
            return True
        if target not in candidates:
            _note("%s：指定预设%s 不可用（未解锁或未配置），跳过" % (name, target))
            _back(context)
            return True
    else:
        if not candidates:
            _note("%s：没有处于「使用」的候选预设（只有一套已配置），报告无可换预设并跳过" % name)
            _back(context)
            return True
        target = candidates[0]
        slot_count = len(before_row.get("slots", []))
        if len(candidates) > 1 and slot_count in (3, 5):
            modal_image = _shot(context)
            clean = []
            for number in candidates:
                red = _modal_row_red(context, modal_image, presets[number].get("header"), slot_count)
                if red is None:
                    clean = candidates
                    sx_note = "弹窗头像读取失败，按第一套候选"
                    break
                if red < mood.SLOT_RED_MIN:
                    clean.append(number)
            if clean:
                target = clean[0]
        else:
            sx_note = "候选唯一或非 3 槽设施，按第一套候选"

    center = presets[target].get("center")
    if not center:
        _save(_shot(context), "fail_center_%s" % name)
        _note("%s：预设%s 按钮中心没读到，停止" % (name, target))
        return False
    _note("%s：点「使用」预设%s%s" % (name, target, ("（%s）" % sx_note) if sx_note else ""))
    _click(context, center[0], center[1])
    _sleep(2.0)

    after_image = _shot(context)
    after_globals = _globals(context, after_image)
    if (before_globals["power"], before_globals["load"]) == \
            (after_globals["power"], after_globals["load"]):
        # 实测：挖掘矿场 预设1↔预设2 电力完全相同（885/1298 电力充裕），
        # 电力不是「预设」的稳定指纹，只能记录，不能当失败判据。
        _note("%s：复核②记录，电力/负载未变化（%s %s→%s %s），按实测同功耗处理，不作为失败"
              % (name, before_globals["power"], before_globals["load"],
                 after_globals["power"], after_globals["load"]))
    else:
        _note("%s：复核②通过 %s %s → %s %s"
              % (name, before_globals["power"], before_globals["load"],
                 after_globals["power"], after_globals["load"]))

    # 复核①：重开弹窗看翻转
    box2, image2 = _locate_row(context, name)
    if not box2:
        _save(after_image, "fail_relocate_%s" % name)
        _note("%s：复核①重定位失败" % name)
        return False
    _click_preset_button(context, box2, image2)
    _sleep(1.5)
    verify = None
    for _ in range(3):
        verify = _parse_presets(context, _shot(context))
        if target in verify:
            break
        _modal_scroll(context)
    if not verify or target not in verify or verify[target].get("button") != "工作中":
        _save(_shot(context), "fail_flip_%s" % name)
        _note("%s：复核①失败，预设%s 文案=%s"
              % (name, target, (verify or {}).get(target, {}).get("button")))
        _back(context)
        return False
    old = working[0] if working else None
    if old and old in verify and verify[old].get("button") != "使用":
        _save(_shot(context), "fail_old_%s" % name)
        _note("%s：复核①失败，原工作预设%s 未转回 使用" % (name, old))
        _back(context)
        return False
    _note("%s：复核①通过 预设%s 使用→工作中%s"
          % (name, target, "，预设%s→使用" % old if old else ""))
    _back(context)

    # 复核③：2.5s 后再抓，整组无红
    _sleep(2.5)
    box3, image3 = _locate_row(context, name)
    if not box3:
        _save(_shot(context), "fail_red_locate_%s" % name)
        _note("%s：复核③重定位失败" % name)
        return False
    after_row = mood.measure_row(mood.image_rgb(image3), name, box3)
    if mood.needs_shift(after_row):
        if name in SINGLE_SLOT_FACILITIES:
            _save(image3, "warn_red_left_%s" % name)
            if len(after_row["red_slots"]) > len(before_row["red_slots"]):
                _note("%s：复核③失败，单槽设施红槽数意外上升 %s→%s"
                      % (name, before_row["red_slots"], after_row["red_slots"]))
                return False
            _note("%s：复核③记录，单槽设施切换后仍红 %s（该角色心情由合成操作瞬时消耗，"
                  "红脸属正常态，不判失败），继续下一设施"
                  % (name, after_row["red_slots"]))
        else:
            _save(image3, "fail_red_left_%s" % name)
            _note("%s：复核③失败，切换后仍有红脸 %s" % (name, after_row["red_slots"]))
            return False
    else:
        _note("%s：复核③通过，整组红比例 %.3f/绿比例 %.3f"
              % (name, after_row["red_ratio"], after_row["green_ratio"]))

    # 复核④：误伤
    current_rows, _boxes = _measure_view(context, image3)
    problems = _mischief(baseline, current_rows, name, before_globals, after_globals, handled)
    if problems:
        _save(image3, "fail_mischief_%s" % name)
        _note("%s：复核④失败，误伤 %s" % (name, problems))
        return False
    _note("%s：复核④通过，其他设施人数/心情态未变；电力 %s→%s"
          % (name, before_globals["power"], after_globals["power"]))
    return True


def run_shift(context) -> bool:
    rows, globs = _scan_panel(context)
    if not any(row.get("state") not in ("unmeasured",) for row in rows.values()):
        _save(_shot(context), "fail_no_panel")
        _note("没认出任何驻员面板行，停止")
        return False
    path, baseline_data = _dump_baseline(rows, globs)
    red_rows = [name for name, row in rows.items() if mood.needs_shift(row)]
    _note("基线：%d 个设施有红脸 %s；红脸槽位 %d 个；基线文件 %s"
          % (len(red_rows), red_rows, len(baseline_data["red"]), path))
    if not red_rows:
        _note("巡检通过：全部绿脸，无需换班（到点检测，不切预设）")
        _report_next_run(swapped=False)
        return True

    preferred = _preferred_presets()
    if preferred:
        _note("interface.json 预设覆盖：%s" % preferred)

    handled = []
    for name in [n for n in mood.PRODUCTION_FACILITIES if n in red_rows]:
        _note("开始处理 %s" % name)
        if not _process_facility(context, name, rows, preferred, handled):
            return False
        handled.append(name)
    _note("本轮换班结束，用时完成")
    _report_next_run(swapped=True)
    return True


# ---------------------------------------------------------------- 人工换人（禁用）

def run_manual_swap(context) -> bool:
    """宿舍专用人工换人骨架：一次一行、每行重新定位。

    角色选择弹窗的点击序列尚未实测，因此不在这里编造坐标；
    节点 基建_换班_人工干预 设为 enabled=false，日常链路永远不会调到。
    """
    image = _shot(context)
    boxes = mood.ocr_name_boxes(context, image)
    box = boxes.get(mood.DORM)
    if not box:
        _note("人工换人：当前界面没看到宿舍行，跳过")
        return True
    _note("人工换人：宿舍行锚点 %s，等待人工确认（路径未实测，未执行任何点击）" % (box,))
    return True


# ---------------------------------------------------------------- 只读巡检（证据用）

def run_idle_probe(context, target="挖掘矿场") -> bool:
    """只读巡检：扫面板 + 打开目标设施的预设弹窗读数，**绝不点「使用」**。

    用途是回答「闲置那套的红脸会不会自己变绿」：工作套的脸在面板里，
    闲置套的脸只在预设弹窗里，所以只能开弹窗看一眼再返回。
    """
    rows, globs = _scan_panel(context)
    if not rows:
        _save(_shot(context), "probe_no_panel")
        _note("巡检：没认出驻员面板行")
        return False
    if target not in rows:
        _note("巡检：面板里没有 %s" % target)
        return False
    box, image = _locate_row(context, target)
    if not box:
        _save(_shot(context), "probe_no_row")
        _note("巡检：定位 %s 失败" % target)
        return False
    _click_preset_button(context, box, image)
    _sleep(1.5)
    modal_image = _shot(context)
    presets = _parse_presets(context, modal_image)
    for _ in range(3):
        if any(row.get("button") == "使用" for row in presets.values()):
            break
        _modal_scroll(context)
        modal_image = _shot(context)
        for number, row in _parse_presets(context, modal_image).items():
            presets.setdefault(number, row)
    _save(modal_image, "probe_modal_%s" % target)
    slot_count = len(rows[target].get("slots", []))
    for number, row in presets.items():
        row["avatar"] = _modal_row_slots(modal_image, row.get("header"), slot_count)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "facility": target,
        "power": globs.get("power"),
        "load": globs.get("load"),
        "staff": globs.get("staff"),
        "rows": rows,
        "presets": {
            str(number): {
                "button": row.get("button"),
                "locked": bool(row.get("locked")),
                "header": row.get("header"),
                "avatar": [
                    {"index": i, "red": slot["red"], "green": slot["green"],
                     "mean_rgb": slot["mean_rgb"]}
                    for i, slot in enumerate(row.get("avatar") or [])
                ],
            }
            for number, row in sorted(presets.items())
        },
    }
    Path(_BASELINE_DIR).mkdir(parents=True, exist_ok=True)
    path = Path(_BASELINE_DIR) / ("idle_probe_%s.json" % stamp)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _note("巡检结果 %s" % path)
    _back(context)
    return True


def _probe_modal(context, facility, box, image):
    """打开预设弹窗，读全部预设的按钮/锁定/头像徽章，然后关窗。只读。"""
    _click_preset_button(context, box, image)
    _sleep(1.5)
    modal_image = _shot(context)
    presets = _parse_presets(context, modal_image)
    for _ in range(3):
        if any(row.get("button") == "使用" for row in presets.values()):
            break
        _modal_scroll(context)
        modal_image = _shot(context)
        for number, row in _parse_presets(context, modal_image).items():
            presets.setdefault(number, row)
    _save(modal_image, "probe_modal_%s" % facility)
    slot_count = len(mood.FACILITY_SLOTS.get(facility, ())) or 3
    for number, row in presets.items():
        row["avatar"] = _modal_row_slots(modal_image, row.get("header"), slot_count)
    _back(context)
    return presets


def _preset_snapshot(presets):
    return {
        str(number): {
            "button": row.get("button"),
            "locked": bool(row.get("locked")),
            "avatar": [
                {"index": i, "red": slot["red"], "green": slot["green"],
                 "mean_rgb": slot["mean_rgb"]}
                for i, slot in enumerate(row.get("avatar") or [])
            ],
        }
        for number, row in sorted(presets.items())
    }


def run_force_preset(context, facility, preset, wait_seconds=0) -> bool:
    """实验用：把指定设施强制切到指定预设，并在切换前后各读一次弹窗。

    只用于测量「在岗消耗 / 闲置恢复」，正常换班链路绝不调用。
    不点 ⚠50、不买、不进宿舍。
    """
    rows, globs = _scan_panel(context)
    if not rows or facility not in rows:
        _save(_shot(context), "force_no_panel")
        _note("强制预设：面板里没有 %s" % facility)
        return False
    box, image = _locate_row(context, facility)
    if not box:
        _save(_shot(context), "force_no_row")
        _note("强制预设：定位 %s 失败" % facility)
        return False

    before = _probe_modal(context, facility, box, image)
    _note("强制预设：切换前 (%s) %s" % (
        facility, {n: r.get("button") for n, r in before.items()}))

    box, image = _locate_row(context, facility)
    if not box:
        return False
    _click_preset_button(context, box, image)
    _sleep(1.5)
    presets = _collect_presets(context)
    if not presets:
        _save(_shot(context), "force_no_modal")
        _note("强制预设：弹窗没读到预设")
        return False
    row = presets.get(preset)
    if not row:
        _save(_shot(context), "force_no_target")
        _note("强制预设：弹窗里没有预设%s" % preset)
        return False
    if row.get("locked"):
        _save(_shot(context), "force_locked")
        _note("强制预设：预设%s 未解锁（⚠50），不点击" % preset)
        return False
    if row.get("button") == "工作中":
        _note("强制预设：预设%s 已在工作中，无需点击" % preset)
    else:
        center = row.get("center")
        if not center:
            _save(_shot(context), "force_no_center")
            _note("强制预设：预设%s 按钮中心没读到" % preset)
            return False
        _click(context, center[0], center[1])
        _note("强制预设：已点击 %s 预设%s 的「使用」" % (facility, preset))
        _sleep(2.0)

    if wait_seconds > 0:
        _note("强制预设：等待 %d 秒" % wait_seconds)
        _sleep(wait_seconds)

    rows2, globs2 = _scan_panel(context)
    if not rows2:
        _save(_shot(context), "force_no_panel_after")
        _note("强制预设：切换后扫描失败")
        return False
    after_box, after_image = _locate_row(context, facility)
    after = _probe_modal(context, facility, after_box, after_image) if after_box else {}
    _note("强制预设：切换后 (%s) %s" % (
        facility, {n: r.get("button") for n, r in after.items()}))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "facility": facility,
        "preset": preset,
        "wait_seconds": wait_seconds,
        "before_power": globs.get("power"), "before_load": globs.get("load"),
        "after_power": globs2.get("power"), "after_load": globs2.get("load"),
        "before_staff": globs.get("staff"), "after_staff": globs2.get("staff"),
        "row_before": rows.get(facility), "row_after": rows2.get(facility),
        "presets_before": _preset_snapshot(before),
        "presets_after": _preset_snapshot(after),
    }
    Path(_BASELINE_DIR).mkdir(parents=True, exist_ok=True)
    path = Path(_BASELINE_DIR) / ("force_preset_%s.json" % stamp)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _note("强制预设结果 %s" % path)
    return True


class InfraShiftAction(CustomAction):
    def run(self, context, argv):
        try:
            return run_shift(context)
        except Exception as exc:  # pragma: no cover
            _note("换班异常：%s" % exc)
            try:
                _save(_shot(context), "fail_exception")
            except Exception:
                pass
            return False


class InfraManualSwapAction(CustomAction):
    def run(self, context, argv):
        return run_manual_swap(context)


class InfraIdleProbeAction(CustomAction):
    """只读巡检，只在 tools/run_task.py 里注册，不进生产 agent。"""

    def run(self, context, argv):
        try:
            return run_idle_probe(context)
        except Exception as exc:  # pragma: no cover
            _note("巡检异常：%s" % exc)
            return False


class InfraForcePresetAction(CustomAction):
    """实验用强制切指定预设（custom_action_param: facility/preset/wait_seconds）。

    只注册在 tools/run_task.py，不进生产 agent；正常换班永远走 infra_shift。
    """

    def run(self, context, argv):
        try:
            param = json.loads(getattr(argv, "custom_action_param", "") or "{}")
        except Exception:
            param = {}
        facility = str(param.get("facility") or "挖掘矿场")
        try:
            preset = int(param.get("preset", 1))
        except Exception:
            preset = 1
        try:
            wait_seconds = int(param.get("wait_seconds", 0))
        except Exception:
            wait_seconds = 0
        try:
            return run_force_preset(context, facility, preset, wait_seconds)
        except Exception as exc:  # pragma: no cover
            _note("强制预设异常：%s" % exc)
            try:
                _save(_shot(context), "force_exception")
            except Exception:
                pass
            return False


# 注册放在 main.py：本模块也会被 tools/run_task.py 进程内导入，
# 那里再导入 maa.agent.agent_server 会把 Library 切到 AgentServer 变体，
# 导致 MaaResourceCreate 不可用。
