"""商店购买引擎（三家店共用）：万物·罕见星尘区 / 角斗场·商店兑换 / 镜像竞技·商店兑换。

设计约束（用户口径 2026-11-06 修订）：
- 一律读 config/purchase_whitelist.json；未列出的 store_key 整店关闭（fail-closed）。
- 只买 enabled=true（或面板勾选）且 currency != paid 的条目；store.enabled 必须为 true。
- 三道闸：① 付费符号扫描（¥/￥/粲晶/△/凭证/充值/RMB）→ ② 白名单 → ③ 余额保底 balance_floor。
  **max_price 只作参考价展示，不再拦截**：三家店是阶梯式涨价，涨价属正常机制，勾了就要买。
- 同名不同价：匹配键为「名字 + 价格」。同名出现多价时，白名单条目必须配 `price` 才买，
  未配则判歧义跳过（fail-closed）；同名只有一个价格时不强制比价（阶梯涨价属正常）。
  多价命中后锚定货架格位，重扫时跟住同一档，不会串到另一档同名商品。
- 余额保底 balance_floor（每店默认 0，可在白名单 store 级覆盖；2026-11-06 用户口径：勾了就买干净）：
  买前 余额-单价 < 保底 → 立即停止该店。保底为 0 时只有「余额 < 单价」才停，记「余额不足」并停止
  （不是跳过继续）；这是唯一的金额类拦截。
- 白名单为空 → 正常跳过并报「未配置可购买条目」，不算失败、不空转点击。
- 买前买后各存一帧，货币差值必须等于售价；对不上即判失败（不假成功）。
- 售罄 / 剩余0 / 货架未见 / 余额不可读 → 跳过或中止，不空点。
- 幂等（P-050）：任务开始写 sandbox/store_run/<store>_<ts>.running，结束改 .done 并写入成交量；
  5 分钟内发现同店 .running/.done → 记「疑似重跑，跳过」并成功退出（不重复买）。
- 只读页面与点击商品卡，不点「碎片兑换」「刷新」「兑换」。

custom_action_param:
  store_key: wtrade_rare | gladiator_shop | mirror_shop
  max_items: 本次最多买几件（默认 1；常规运行由 pipeline 调大到 20，勾了即买空）
  dry_run:   true 时只扫描与判定，不点击购买（默认 false，且不写幂等标记）
"""
import json
import logging
import os
import re
import time
from pathlib import Path

from maa.custom_action import CustomAction
from maa.context import Context

log = logging.getLogger("laa.store")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WHITELIST_PATH = PROJECT_ROOT / "config" / "purchase_whitelist.json"
INSTANCES_DIR = PROJECT_ROOT / "config" / "instances"
FRAME_DIR = PROJECT_ROOT / "debug" / "needframes"

OCR_NODE = "通用_OCR探针"
PAID_TOKENS = ("¥", "￥", "粲晶", "△", "凭证", "充值", "RMB", "美元")

# 余额保底：买前「余额-单价 < balance_floor」即停止该店（唯一金额类拦截）
# 2026-11-06 用户口径：默认 0（买干净），余额不够付才停
DEFAULT_BALANCE_FLOOR = 0
# 幂等标记目录（与 needframes 同级的 sandbox/store_run，三份副本共享同一目录）
RUN_DIR = FRAME_DIR.parent / "store_run"
IDEMPOTENT_WINDOW_SEC = 300

# 货架版式（坐标来自 2026-11-05 19:10/19:17/19:20 实机帧 OCR box）
STORE_LAYOUT = {
    "wtrade_rare": {
        "label": "万物贸易所-罕见星尘区",
        "entry": "罕见星尘购买",
        "option": "商店购买_罕见星尘区_物品选择",
        "balance_roi": [1160, 20, 120, 45],
        # 付费符号页级扫描只覆盖货架网格：左侧导航的「充值」按钮在 x<215，
        # 顶部页签可能滚出「粲晶贸易区」，都属页面固定 chrome，不是商品价格。
        "grid_roi": [250, 130, 1030, 530],
        "ignore_rois": [[0, 110, 215, 540], [0, 45, 1280, 85]],
        "columns": [355, 571, 787, 1003, 1219],
        "cell_w": 200,
        "pages": [
            {"name_y": 310, "price_y": 351, "remain_y": 287},
            {"name_y": 548, "price_y": 590, "remain_y": 526},
        ],
        "max_pages": 2,
    },
    "gladiator_shop": {
        "label": "角斗场-商店兑换",
        "entry": "角斗场商店购买",
        "option": "角斗场兑换_物品选择",
        "balance_roi": [1160, 20, 120, 45],
        "grid_roi": [0, 165, 1280, 505],
        "ignore_rois": [[0, 45, 1280, 115]],
        "columns": [135, 351, 566, 782, 999],
        "cell_w": 200,
        "pages": [
            {"name_y": 337, "price_y": 379, "remain_y": 316},
            {"name_y": 575, "price_y": 618, "remain_y": 554},
        ],
        "max_pages": 1,
    },
    "mirror_shop": {
        "label": "镜像竞技-商店兑换",
        "entry": "镜像竞技商店购买",
        "option": "镜像竞技兑换_物品选择",
        "balance_roi": [1160, 20, 120, 45],
        "grid_roi": [0, 170, 1280, 500],
        "ignore_rois": [[0, 45, 1280, 115]],
        "columns": [135, 351, 566, 782, 998, 1213],
        "cell_w": 200,
        "pages": [
            {"name_y": 337, "price_y": 379, "remain_y": 316},
            {"name_y": 575, "price_y": 618, "remain_y": 554},
        ],
        "max_pages": 1,
    },
}

PAGE_SWIPE = ((950, 400), (250, 400), 500)


def _ocr(context: Context, roi, image=None):
    """返回 [(text, box)]；box 为 [x,y,w,h]。"""
    if image is None:
        image = context.tasker.controller.post_screencap().wait().get()
    result = context.run_recognition(
        OCR_NODE, image,
        pipeline_override={
            OCR_NODE: {
                "recognition": "OCR",
                "roi": list(roi),
                "expected": [],
            }
        },
    )
    items = []
    if not result:
        return items
    candidates = [getattr(result, "best_result", None)]
    candidates.extend(getattr(result, "all_results", None) or [])
    for item in candidates:
        text = getattr(item, "text", None)
        if text is None:
            continue
        items.append((str(text), list(getattr(item, "box", []) or [])))
    return items


def _first_number(text: str):
    nums = re.findall(r"\d+", text.replace(",", ""))
    if not nums:
        return None
    return int(nums[-1])


def _center(box):
    if not box or len(box) < 4:
        return None
    return int(box[0] + box[2] // 2), int(box[1] + box[3] // 2)


def _save_frame(context: Context, tag: str):
    try:
        FRAME_DIR.mkdir(parents=True, exist_ok=True)
        image = context.tasker.controller.post_screencap().wait().get()
        path = FRAME_DIR / f"store_{tag}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        try:
            import cv2
            cv2.imwrite(str(path), image)
        except Exception:
            from PIL import Image
            Image.fromarray(image[:, :, ::-1]).save(path)
        return path
    except Exception as exc:  # 存帧失败不影响主流程
        log.warning("存帧失败 %s: %s", tag, exc)
        return None


def _load_whitelist():
    data = json.loads(WHITELIST_PATH.read_text(encoding="utf-8"))
    stores = data.get("stores")
    if not isinstance(stores, dict):
        raise ValueError("purchase_whitelist.json 缺少 stores")
    return stores


def _instance_config_path():
    """当前 MFA 实例配置（与 alien_shop_marker.instance_config_path 同口径）。"""
    configured = (
        os.environ.get("MAA_INSTANCE_CONFIG", "").strip()
        or os.environ.get("MFA_INSTANCE_CONFIG_PATH", "").strip()
    )
    instance_id = os.environ.get("MFA_INSTANCE_ID", "").strip()
    candidates = [
        Path(configured) if configured else None,
        INSTANCES_DIR / (instance_id + ".json") if instance_id else None,
        INSTANCES_DIR / "default.json",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    files = sorted(INSTANCES_DIR.glob("*.json"))
    return files[0] if len(files) == 1 else None


def _panel_selected(layout: dict):
    """读面板「物品选择」勾选的商品名。

    返回 None 表示没有勾选（按白名单 enabled=true 走）；返回非空列表表示本次只买这些。
    空 selected_cases 与没有该选项同义：回退到白名单，保持旧行为。
    """
    entry = layout.get("entry")
    option_name = layout.get("option")
    if not entry or not option_name:
        return None
    path = _instance_config_path()
    if not path:
        return None
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        log.warning("读取实例配置失败（按白名单 enabled 处理）：%s", exc)
        return None
    for task in cfg.get("TaskItems") or []:
        if task.get("entry") != entry:
            continue
        for option in task.get("option") or []:
            if option.get("name") != option_name:
                continue
            picked = [str(name) for name in (option.get("selected_cases") or [])]
            log.info("面板勾选 %d 项：%s", len(picked), picked)
            return picked or None
    return None


def _enabled_items(store: dict, selected=None):
    """本次要买的条目。

    selected 非空 -> 只买这些名字（面板优先），但每个名字仍要在白名单里登记过
    （未登记 = 不是白名单商品，fail-closed 拒绝）；为空 -> 沿用白名单 enabled=true。
    两种情况都要过 currency 校验；max_price 只作参考价，不参与拦截；
    price 只用于同名多价时区分档位，同样不参与金额拦截。
    """
    items = store.get("items") or []
    if selected:
        known = {str(item.get("name")) for item in items}
        unknown = [name for name in selected if name not in known]
        if unknown:
            log.error("面板勾选了白名单未登记的商品，拒绝购买：%s", unknown)
        picked = [item for item in items if str(item.get("name")) in selected]
    else:
        picked = [item for item in items if item.get("enabled")]

    out = []
    for item in picked:
        currency = str(item.get("currency") or "").strip().lower()
        if currency not in ("free", "ingame", "paid"):
            log.error("白名单条目 %s 的 currency 非法：%r，按 paid 处理", item.get("name"), currency)
            continue
        if currency == "paid":
            log.error("白名单条目 %s 标为 paid，违背 fail-closed，跳过", item.get("name"))
            continue
        out.append(item)
    return out


def _balance_floor(store: dict) -> int:
    """store 级余额保底，缺省 500；值非法时回退缺省（不因配置错误放开闸）。"""
    value = store.get("balance_floor", DEFAULT_BALANCE_FLOOR)
    try:
        floor = int(value)
    except (TypeError, ValueError):
        floor = -1
    if floor < 0:
        log.warning("balance_floor 非法 %r，回退默认 %s", value, DEFAULT_BALANCE_FLOOR)
        return DEFAULT_BALANCE_FLOOR
    return floor


def _recent_marker(store_key: str):
    """5 分钟内同店的 .running/.done 标记；没有返回 None。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for path in sorted(RUN_DIR.glob(f"{store_key}_*")):
        if path.suffix not in (".running", ".done"):
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if 0 <= age <= IDEMPOTENT_WINDOW_SEC:
            return path
    return None


def _begin_run(store_key: str):
    """写本次运行的 .running 标记，并清理 24h 前的旧标记。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for path in RUN_DIR.glob("*"):
        try:
            if now - path.stat().st_mtime > 86400:
                path.unlink()
        except OSError:
            continue
    marker = RUN_DIR / f"{store_key}_{time.strftime('%Y%m%d_%H%M%S')}.running"
    marker.write_text(f"start={time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
    return marker


def _finish_run(marker, bought: int, note: str = ""):
    """收尾：补齐成交量并把 .running 改名为 .done（供 5 分钟内重跑判定）。"""
    if marker is None:
        return
    try:
        text = marker.read_text(encoding="utf-8") if marker.exists() else ""
        text += f"end={time.strftime('%Y-%m-%d %H:%M:%S')}\nbought={bought}\n"
        if note:
            text += f"note={note}\n"
        marker.write_text(text, encoding="utf-8")
        marker.rename(marker.with_suffix(".done"))
    except OSError as exc:
        log.warning("幂等标记收尾失败 %s: %s", marker, exc)


def _read_balance(context: Context, roi):
    texts = _ocr(context, roi)
    for text, _box in texts:
        value = _first_number(text)
        if value is not None:
            return value
    return None


def _scan_page(context: Context, layout: dict):
    """扫描当前货架一页，返回条目列表（同名商品各占一条，不再互相覆盖）。

    每条：{name, price, remain, soldout, center, raw_price, page_row, page_col}
    """
    image = context.tasker.controller.post_screencap().wait().get()
    found = []
    for row_index, row in enumerate(layout["pages"]):
        for col_index, col in enumerate(layout["columns"]):
            half = layout["cell_w"] // 2
            name_items = _ocr(context, [col - half, row["name_y"] - 18, layout["cell_w"], 36], image)
            price_items = _ocr(context, [col - half, row["price_y"] - 22, layout["cell_w"], 44], image)
            remain_items = _ocr(context, [col - half, row["remain_y"] - 17, layout["cell_w"], 34], image)

            name = None
            for text, box in name_items:
                if re.search(r"[\u4e00-\u9fa5A-Za-z]", text):
                    name = text.replace(" ", "")
                    name_box = box
                    break
            if not name:
                continue

            joined_price = " ".join(t for t, _ in price_items)
            soldout = "售罄" in joined_price or "已售" in joined_price
            price = None
            for text, _box in price_items:
                if "售罄" in text or "已售" in text:
                    continue
                value = _first_number(text)
                if value is not None:
                    price = value
                    break

            remain = None
            for text, _box in remain_items:
                if "剩余" in text:
                    remain = _first_number(text)
                    break

            center = _center(name_box) or (col, row["name_y"])
            found.append({
                "name": name,
                "price": price,
                "remain": remain,
                "soldout": soldout,
                "center": center,
                "raw_price": joined_price.strip(),
                "page_row": row_index,
                "page_col": col_index,
            })
    return found


def _resolve_shelf(shelf, items, anchors=None, page=0):
    """按「名字 + 价格」把白名单条目解析到货架条目。

    返回 [(items 下标, 白名单条目, 货架条目)]，保持白名单顺序。
    - 同名且整页只有一个价格：直接命中（阶梯涨价属正常，不拿价格拦截）；
    - 同名出现多价：白名单条目必须配 `price` 才命中，否则判歧义跳过（fail-closed）；
    - 多价命中后锚定 (页, 行, 列) 格位，后续重扫按格位跟随阶梯涨价，
      不会因为涨价跑到另一档同名商品上。
    """
    anchors = anchors if anchors is not None else {}
    resolved = []
    for index, item in enumerate(items):
        name = str(item.get("name") or "")
        cands = [entry for entry in shelf if entry.get("name") == name]
        if not cands:
            continue
        prices = sorted({entry["price"] for entry in cands if entry.get("price") is not None})
        if len(prices) <= 1:
            resolved.extend((index, item, entry) for entry in cands)
            continue

        want = item.get("price")
        if want is None:
            log.error("%s 同名商品出现多档价格 %s，白名单未配置 price，判歧义并跳过（fail-closed）",
                      name, prices)
            continue
        try:
            want = int(want)
        except (TypeError, ValueError):
            log.error("%s 白名单 price=%r 非法，判歧义并跳过（fail-closed）", name, item.get("price"))
            continue

        pinned = anchors.get(index)
        if pinned:
            # 已锚定格位：涨价后价格不再是配置值，但格位不变，仍跟住同一档
            hit = next((entry for entry in cands
                        if (page, entry["page_row"], entry["page_col"]) == pinned), None)
            if hit is not None:
                resolved.append((index, item, hit))
                continue

        tier = [entry for entry in cands if entry.get("price") == want]
        if not tier:
            log.error("%s 同名商品货架价格 %s，白名单配置 price=%s 无对应档位，跳过（fail-closed）",
                      name, prices, want)
            continue
        resolved.extend((index, item, entry) for entry in tier)
    return resolved


def _ocr_screen_text(context: Context):
    return _ocr(context, [0, 0, 1280, 720])


def _inside(box, roi) -> bool:
    """box=[x,y,w,h] 的中心点是否落在 roi 内。"""
    if not box or len(box) < 4:
        return False
    cx = box[0] + box[2] / 2
    cy = box[1] + box[3] / 2
    x, y, w, h = roi
    return x <= cx <= x + w and y <= cy <= y + h


def _paid_hits(texts, layout):
    """付费符号命中（排除页面固定 chrome：左侧导航、顶部页签）。

    只用来过滤「充值」「粲晶贸易区」这类非商品文本；弹窗正文（居中）不受影响。
    """
    ignore = layout.get("ignore_rois") or []
    hits = []
    for text, box in texts:
        for token in PAID_TOKENS:
            if token not in text:
                continue
            if any(_inside(box, roi) for roi in ignore):
                break
            hits.append(token)
            break
    return hits


def _close_modal(context: Context):
    context.tasker.controller.post_click_key(4).wait()  # BACK
    time.sleep(0.6)


def _click(context: Context, point, delay=0.9):
    context.tasker.controller.post_click(int(point[0]), int(point[1])).wait()
    time.sleep(delay)


def _buy_one(context: Context, entry, layout: dict) -> str:
    """对单个货架条目执行一次购买。返回状态字符串。"""
    _click(context, entry["center"], 1.2)

    modal_texts = _ocr_screen_text(context)
    joined = " ".join(t for t, _ in modal_texts)

    # 闸①：弹窗里出现付费符号 → 立即退出，不点购买（忽略页面固定导航文本）
    hit_paid = _paid_hits(modal_texts, layout)
    if hit_paid:
        log.error("弹窗命中付费符号 %s，放弃购买", hit_paid)
        _close_modal(context)
        return "paid"

    if "已售罄" in joined or "售罄" in joined:
        log.info("弹窗显示已售罄，跳过")
        _close_modal(context)
        return "soldout"

    buy_btn = None
    for text, box in modal_texts:
        if text.strip() in ("购买", "确认购买"):
            buy_btn = _center(box)
            break
    if not buy_btn:
        log.error("弹窗未找到「购买」按钮，放弃（不盲点）")
        _close_modal(context)
        return "no_button"

    _click(context, buy_btn, 1.8)

    # 关掉「获得物品」
    for text, box in _ocr_screen_text(context):
        if "获得物品" in text:
            point = _center(box)
            if point:
                _click(context, point, 0.9)
            break
    for text, box in _ocr_screen_text(context):
        if text.strip() in ("确定", "确认"):
            point = _center(box)
            if point:
                _click(context, point, 0.8)
            break
    return "bought"


class StorePurchaseAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        raw = getattr(argv, "custom_action_param", None) or "{}"
        try:
            param = json.loads(raw)
            if isinstance(param, str):
                param = json.loads(param)
        except (json.JSONDecodeError, TypeError):
            param = {}
        if not isinstance(param, dict):
            param = {}

        store_key = str(param.get("store_key") or "").strip()
        max_items = int(param.get("max_items") or 1)
        dry_run = bool(param.get("dry_run"))

        layout = STORE_LAYOUT.get(store_key)
        if not layout:
            log.error("未知 store_key：%r（仅支持 %s）", store_key, list(STORE_LAYOUT))
            return False

        try:
            stores = _load_whitelist()
        except Exception as exc:
            log.error("白名单读取失败：%s", exc)
            return False

        store = stores.get(store_key)
        if not isinstance(store, dict) or not store.get("enabled"):
            log.info("[%s] 商店未在白名单中启用，整店关闭（跳过）", layout["label"])
            return True

        balance_floor = _balance_floor(store)

        # 幂等（P-050）：同一条命令沙箱内外会各跑一次，5 分钟内同店已有标记即按重跑处理。
        marker = None
        if not dry_run:
            recent = _recent_marker(store_key)
            if recent:
                log.info("[%s] 疑似重跑，跳过（发现 %s，%d 秒内），不重复购买",
                         layout["label"], recent.name, IDEMPOTENT_WINDOW_SEC)
                return True
            marker = _begin_run(store_key)

        bought = 0
        note = ""
        try:
            selected = _panel_selected(layout)
            items = _enabled_items(store, selected)
            if not items:
                # 跳过也要记账：读一次余额，供跑后核对「未发生扣费」
                balance = _read_balance(context, layout["balance_roi"])
                if selected:
                    log.info("[%s] 面板勾选了 %d 项，但都不在白名单可购买范围内，跳过（非失败）；当前余额=%s",
                             layout["label"], len(selected), balance)
                else:
                    log.info("[%s] 未配置可购买条目，跳过（非失败）；当前余额=%s",
                             layout["label"], balance)
                _save_frame(context, f"{store_key}_skip")
                return True
            log.info("[%s] 可购买条目：%s（price 用于同名多价区分；max_price 仅参考展示，不拦截）",
                     layout["label"], [(i.get("name"), i.get("price"), i.get("max_price")) for i in items])
            log.info("[%s] 余额保底：%s", layout["label"], balance_floor)

            balance_before = _read_balance(context, layout["balance_roi"])
            log.info("[%s] 买前余额：%s", layout["label"], balance_before)
            _save_frame(context, f"{store_key}_before")

            page = 0
            anchors = {}  # 白名单下标 -> (页, 行, 列)，同名多价命中后跟住同一档
            while page < layout["max_pages"] and bought < max_items:
                shelf = _scan_page(context, layout)
                log.info("[%s] 第 %d 页货架 %d 件：%s", layout["label"], page + 1, len(shelf),
                         [(entry["name"], entry["price"], entry["remain"]) for entry in shelf])

                # 闸①（页面级）：货架网格出现付费符号 → 整店中止（不含导航/页签 chrome）
                grid_roi = layout.get("grid_roi")
                page_texts = _ocr(context, grid_roi) if grid_roi else _ocr_screen_text(context)
                hit_paid = _paid_hits(page_texts, layout)
                if hit_paid:
                    log.error("[%s] 页面出现付费符号 %s，整店中止", layout["label"], hit_paid)
                    _save_frame(context, f"{store_key}_paid")
                    note = f"paid_hit={hit_paid}"
                    return False

                candidates = []
                floor_hit = False
                for index, item, entry in _resolve_shelf(shelf, items, anchors, page):
                    name = str(item.get("name") or "")
                    if entry["soldout"] or entry["remain"] == 0:
                        log.info("[%s] %s 价 %s 已售罄/剩余0，跳过", layout["label"], name, entry["price"])
                        continue
                    price = entry["price"]
                    if price is None:
                        log.warning("[%s] %s 价格不可读，跳过（不猜）", layout["label"], name)
                        continue
                    if balance_before is None:
                        log.error("[%s] 余额不可读，无法核对，终止", layout["label"])
                        note = "balance_unreadable"
                        return False
                    # 闸③：余额保底（唯一金额类拦截）；保底 0 时等价于「余额不够付」
                    if balance_before - price < balance_floor:
                        if balance_before < price:
                            log.info("[%s] %s 单价 %s，余额 %s 不足，余额不足，停止",
                                     layout["label"], name, price, balance_before)
                            note = f"insufficient name={name} price={price} balance={balance_before}"
                        else:
                            log.info("[%s] %s 单价 %s，余额 %s - 单价 < 保底 %s，余额触及保底，停止",
                                     layout["label"], name, price, balance_before, balance_floor)
                            note = f"floor_hit name={name} price={price} balance={balance_before}"
                        floor_hit = True
                        break
                    candidates.append((price, name, entry, item.get("max_price"), index))

                if floor_hit:
                    break

                # 按白名单顺序买（先买靠前条目），不用价格排序：
                # 否则便宜的条目会把余额吃光，用户排在前面的目标反而买不到。
                # 2026-11-06 实跑：角斗场勾了 高纯硅化剂+破碎程式x50，价格升序会一直买便宜的破碎程式。
                purchased = False
                for price, name, entry, ref_price, index in candidates:
                    if bought >= max_items:
                        break
                    if dry_run:
                        log.info("[%s] dry_run：将买 %s 价 %s（参考价 %s）", layout["label"], name,
                                 price, ref_price)
                        continue
                    status = _buy_one(context, entry, layout)
                    log.info("[%s] %s 购买结果：%s", layout["label"], name, status)
                    if status != "bought":
                        continue

                    _save_frame(context, f"{store_key}_after")
                    balance_after = _read_balance(context, layout["balance_roi"])
                    if balance_after is None:
                        log.error("[%s] %s 买后余额不可读，无法核对差值，判失败", layout["label"], name)
                        note = f"balance_unreadable_after name={name}"
                        return False
                    delta = balance_before - balance_after
                    if delta != price:
                        log.error("[%s] %s 余额差值 %s ≠ 售价 %s，判失败", layout["label"], name, delta, price)
                        note = f"delta_mismatch name={name} price={price} delta={delta}"
                        return False
                    log.info("[%s] %s 购买成功：单价 %s，余额 %s → %s（-%s）", layout["label"], name,
                             price, balance_before, balance_after, delta)
                    balance_before = balance_after
                    bought += 1
                    purchased = True
                    # 同名多价时记住货架格位：涨价后仍跟住同一档，不串到另一档
                    anchors[index] = (page, entry["page_row"], entry["page_col"])
                    break  # 买完一件重扫当前页（剩余数与阶梯价都会变）

                if purchased:
                    continue

                page += 1
                if page < layout["max_pages"]:
                    context.tasker.controller.post_swipe(*PAGE_SWIPE[0], *PAGE_SWIPE[1], PAGE_SWIPE[2]).wait()
                    time.sleep(1.0)

            if bought == 0:
                log.info("[%s] 本轮未购买（无可买/已售罄/触及保底），正常结束", layout["label"])
            else:
                log.info("[%s] 本轮共购买 %d 件", layout["label"], bought)
            return True
        finally:
            _finish_run(marker, bought, note)
