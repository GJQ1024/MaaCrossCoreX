# -*- coding: utf-8 -*-
"""基建驻员心情检测（红脸 / 绿脸）—— 触发模块，只回答「谁要换」。

实测事实（2026-11-05，1280x720）：
  * 心情只显示在「驻员状况」面板里，每个头像左下角一个圆形徽章：
    绿笑脸 / 红哭脸；主界面建筑上没有任何心情标记。
  * 徽章列：3 槽行相对设施名框左边 +344/+439/+533；5 槽行 +157/+252/+347/+442/+537。
  * 徽章中心相对设施名框顶边 0px（实测 −3..+3）。
  * 行高 139；面板行序：行星指挥部、能源发电站、挖掘矿场、原料交易所、
    合成工厂、研发中心、宿舍（最后一行，无「驻员预设」按钮）。
  * 30 帧 × 14 槽稳定性实测：逐槽 RGB 均值波动 0，整行尺度也一样。

判据（用户定的比值，不用 9×9 均值）：
  红  R-G>=50 且 R-B>=30 且 R>=100
  绿  G-R>=60 且 G-B>=10 且 G>=110
  统计口径 = 整行所有槽位徽章圆内的像素占比；任一处红即算该行要换。
"""

from __future__ import annotations

import numpy as np

REF_W = 1280
REF_H = 720
BADGE_SIZE_REF = 27
BADGE_RADIUS_REF = 12
SLOT_DY_REF = 0

SLOT_X_3 = (344, 439, 533)
SLOT_X_5 = (157, 252, 347, 442, 537)

# 整行红色像素占比下限（实测红行 0.600-0.659，绿行红占比 0.000，中间无样本）
ROW_RED_MIN = 0.30
# 单个槽位红色占比下限（实测红槽 0.53-0.90，绿槽 0.00）
SLOT_RED_MIN = 0.30
# 整行绿色占比下限（实测绿行 0.439，红行 0.000）
ROW_GREEN_MIN = 0.25

FACILITY_SLOTS = {
    "行星指挥部": SLOT_X_3,
    "能源发电站": SLOT_X_3,
    "挖掘矿场": SLOT_X_5,
    "原料交易所": SLOT_X_3,
    "合成工厂": SLOT_X_3,
    "研发中心": SLOT_X_3,
    # 宿舍行没有心情徽章，槽位排布也与生产设施不同（可用槽 5 个，其余 5 位不放人也不回体力，
    # 用户 2026-11-06 确认；见 P-041），这里只登记名字，不做颜色测量。
    "宿舍": (),
}
FACILITY_NAMES = tuple(FACILITY_SLOTS)
PRODUCTION_FACILITIES = tuple(n for n in FACILITY_NAMES if n != "宿舍")
DORM = "宿舍"

_MASK_CACHE: dict[tuple[int, int], np.ndarray] = {}


def image_rgb(image, already_rgb=False):
    """MaaFramework 控制器给的是 BGR；PIL 读进来的是 RGB。"""
    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=2)
    arr = arr[:, :, :3]
    if already_rgb:
        return arr.astype(np.int16)
    return arr[:, :, ::-1].astype(np.int16)


def _circle_mask(size: int, radius: int) -> np.ndarray:
    key = (size, radius)
    mask = _MASK_CACHE.get(key)
    if mask is None:
        center = (size - 1) / 2.0
        yy, xx = np.mgrid[0:size, 0:size]
        mask = (xx - center) ** 2 + (yy - center) ** 2 <= radius * radius
        _MASK_CACHE[key] = mask
    return mask


def classify_pixels(rgb):
    """返回 (红掩码, 绿掩码)。rgb 必须是 RGB 顺序。"""
    r = rgb[..., 0]
    g = rgb[..., 1]
    b = rgb[..., 2]
    red = (r - g >= 50) & (r - b >= 30) & (r >= 100)
    green = (g - r >= 60) & (g - b >= 10) & (g >= 110)
    return red, green


def _scaled_slot_centers(box, offsets, width, height):
    sx = width / float(REF_W)
    sy = height / float(REF_H)
    x0, y0 = float(box[0]), float(box[1])
    return [
        (int(round(x0 + dx * sx)), int(round(y0 + SLOT_DY_REF * sy)))
        for dx in offsets
    ]


def measure_slot(rgb, center, scale):
    size = max(9, int(round(BADGE_SIZE_REF * scale)))
    radius = max(4, int(round(BADGE_RADIUS_REF * scale)))
    half = size // 2
    cx, cy = center
    x0 = max(0, cx - half)
    y0 = max(0, cy - half)
    patch = rgb[y0:y0 + size, x0:x0 + size]
    if patch.shape[0] != size or patch.shape[1] != size:
        return None
    mask = _circle_mask(size, radius)
    red, green = classify_pixels(patch)
    total = int(mask.sum())
    if total == 0:
        return None
    mean = patch.reshape(-1, 3)[mask.reshape(-1)].mean(axis=0)
    return {
        "center": [int(cx), int(cy)],
        "mean_rgb": [int(v) for v in mean],
        "red": round(float((red & mask).sum()) / total, 3),
        "green": round(float((green & mask).sum()) / total, 3),
    }


def measure_row(rgb, name, name_box):
    """以 OCR 设施名框为行锚点，返回这一行的心情测量结果。"""
    height, width = rgb.shape[:2]
    offsets = FACILITY_SLOTS.get(name, ())
    row = {
        "facility": name,
        "name_box": [int(v) for v in name_box],
        "slots": [],
        "red_ratio": 0.0,
        "green_ratio": 0.0,
        "state": "unmeasured",
        "red_slots": [],
        "green_slots": [],
    }
    if not offsets:
        return row
    scale = width / float(REF_W)
    centers = _scaled_slot_centers(name_box, offsets, width, height)
    slots = []
    red_px = green_px = total_px = 0
    for index, center in enumerate(centers):
        slot = measure_slot(rgb, center, scale)
        if slot is None:
            continue
        slot["index"] = index
        slots.append(slot)
        size = max(9, int(round(BADGE_SIZE_REF * scale)))
        radius = max(4, int(round(BADGE_RADIUS_REF * scale)))
        n = int(_circle_mask(size, radius).sum())
        total_px += n
        red_px += int(round(slot["red"] * n))
        green_px += int(round(slot["green"] * n))
    row["slots"] = slots
    if total_px:
        row["red_ratio"] = round(red_px / float(total_px), 3)
        row["green_ratio"] = round(green_px / float(total_px), 3)
    row["red_slots"] = [s["index"] for s in slots if s["red"] >= SLOT_RED_MIN]
    row["green_slots"] = [s["index"] for s in slots if s["green"] >= SLOT_RED_MIN]
    if row["red_slots"] or row["red_ratio"] >= ROW_RED_MIN:
        row["state"] = "red"
    elif row["green_ratio"] >= ROW_GREEN_MIN:
        row["state"] = "green"
    else:
        row["state"] = "unknown"
    return row


def needs_shift(row) -> bool:
    """触发规则：只要这一行有任何一个红脸就整组换。绿脸不动。"""
    return row.get("state") == "red"


def _normalize(text: str) -> str:
    return "".join(str(text or "").split())


def match_facility(text: str):
    """把 OCR 文本对齐到设施名；允许 OCR 漏字的短文本。"""
    raw = _normalize(text)
    if len(raw) < 2:
        return None
    for name in FACILITY_NAMES:
        if name in raw:
            return name
    for name in FACILITY_NAMES:
        if len(raw) >= 3 and raw in name:
            return name
    return None


def ocr_name_boxes(context, image, node: str = "基建_驻员状况", roi=None):
    """OCR 只扫左侧设施名列，返回 {设施名: [x,y,w,h]}。

    只扫左侧列可以排除背景里「交易所」这类场景文字造成的假行。
    """
    height, width = np.asarray(image).shape[:2]
    sx = width / float(REF_W)
    sy = height / float(REF_H)
    if roi is None:
        roi = [int(300 * sx), int(60 * sy), int(170 * sx), int(620 * sy)]
    detail = context.run_recognition(
        node,
        image,
        pipeline_override={node: {
            "recognition": "OCR",
            "roi": roi,
            "expected": [],
            "threshold": 0.2,
        }},
    )
    if detail is None or not getattr(detail, "hit", False):
        return {}
    best = {}
    for item in getattr(detail, "all_results", []) or []:
        text = _normalize(getattr(item, "text", ""))
        name = match_facility(text)
        if not name:
            continue
        box = list(getattr(item, "box", []) or [])
        if len(box) < 4:
            continue
        if not (int(roi[0]) - 5 <= box[0] <= int(roi[0]) + int(roi[2]) + 5):
            continue
        score = float(getattr(item, "score", 0) or 0)
        if name not in best or score > best[name][0]:
            best[name] = (score, [int(v) for v in box[:4]])
    return {name: box for name, (_, box) in best.items()}


def measure_frame(context, image=None, node: str = "基建_驻员状况"):
    """OCR 行锚点 + 测量。返回 (rows, image_rgb)。"""
    if image is None:
        image = context.tasker.controller.post_screencap().wait().get()
    rgb = image_rgb(image)
    boxes = ocr_name_boxes(context, image, node)
    rows = {}
    for name, box in boxes.items():
        rows[name] = measure_row(rgb, name, box)
    return rows, rgb
