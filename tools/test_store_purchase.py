"""Offline checks for name+price shelf resolution in store_purchase."""
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import store_purchase as store  # noqa: E402


def entry(name, price, row=0, col=0, soldout=False, remain=1):
    return {
        "name": name,
        "price": price,
        "remain": remain,
        "soldout": soldout,
        "center": (col, row),
        "raw_price": "" if price is None else str(price),
        "page_row": row,
        "page_col": col,
    }


def names(resolved):
    return [(item.get("name"), item.get("price"), e["price"]) for _i, item, e in resolved]


def test_unique_price_name_only_is_bought():
    items = [{"name": "构建蓝图"}]
    shelf = [entry("构建蓝图", 180)]
    resolved = store._resolve_shelf(shelf, items)
    assert names(resolved) == [("构建蓝图", None, 180)]


def test_unique_price_keeps_buying_after_ladder_raise():
    items = [{"name": "构建蓝图"}]
    shelf = [entry("构建蓝图", 240)]  # 第二件涨价，仍应命中
    resolved = store._resolve_shelf(shelf, items)
    assert names(resolved) == [("构建蓝图", None, 240)]


def test_same_name_multi_price_without_price_is_ambiguous():
    items = [{"name": "数据协议"}]
    shelf = [entry("数据协议", 10, col=0), entry("数据协议", 30, col=1)]
    assert store._resolve_shelf(shelf, items) == []


def test_same_name_multi_price_with_price_picks_tier():
    items = [{"name": "数据协议", "price": 30}]
    shelf = [entry("数据协议", 10, col=0), entry("数据协议", 30, col=1)]
    resolved = store._resolve_shelf(shelf, items)
    assert names(resolved) == [("数据协议", 30, 30)]


def test_same_name_multi_price_missing_tier_is_skipped():
    items = [{"name": "数据协议", "price": 500}]
    shelf = [entry("数据协议", 10, col=0), entry("数据协议", 30, col=1)]
    assert store._resolve_shelf(shelf, items) == []


def test_invalid_price_is_skipped():
    items = [{"name": "数据协议", "price": "三十"}]
    shelf = [entry("数据协议", 10, col=0), entry("数据协议", 30, col=1)]
    assert store._resolve_shelf(shelf, items) == []


def test_pinned_slot_follows_ladder_raise():
    items = [{"name": "数据协议", "price": 30}]
    anchors = {0: (0, 0, 2)}
    shelf = [
        entry("数据协议", 10, col=0),
        entry("数据协议", 45, col=2),  # 命中档位涨价，格位不变
    ]
    resolved = store._resolve_shelf(shelf, items, anchors, page=0)
    assert names(resolved) == [("数据协议", 30, 45)]


def test_pinned_slot_missing_falls_back_to_price():
    items = [{"name": "数据协议", "price": 30}]
    anchors = {0: (0, 0, 9)}
    shelf = [entry("数据协议", 10, col=0), entry("数据协议", 30, col=1)]
    resolved = store._resolve_shelf(shelf, items, anchors, page=0)
    assert names(resolved) == [("数据协议", 30, 30)]


def test_duplicate_same_price_cards_are_both_kept():
    items = [{"name": "R4升级芯片"}]
    shelf = [
        entry("R4升级芯片", 720, col=0),
        entry("R4升级芯片", 720, col=1, soldout=True, remain=0),
    ]
    resolved = store._resolve_shelf(shelf, items)
    assert len(resolved) == 2
    assert [e["soldout"] for _i, _item, e in resolved] == [False, True]


def test_multi_price_still_ambiguous_when_cheap_tier_sold_out():
    items = [{"name": "数据协议"}]
    shelf = [
        entry("数据协议", 10, col=0, soldout=True, remain=0),
        entry("数据协议", 30, col=1),
    ]
    assert store._resolve_shelf(shelf, items) == []


def test_whitelist_order_is_preserved():
    items = [{"name": "破碎程式x50"}, {"name": "高纯硅化剂"}]
    shelf = [entry("高纯硅化剂", 1200, col=0), entry("破碎程式x50", 600, col=1)]
    resolved = store._resolve_shelf(shelf, items)
    assert names(resolved) == [("破碎程式x50", None, 600), ("高纯硅化剂", None, 1200)]


def test_scan_page_keeps_duplicate_names():
    layout = {
        "columns": [100, 300],
        "cell_w": 200,
        "pages": [{"name_y": 100, "price_y": 150, "remain_y": 80}],
    }

    class _Shot:
        def wait(self):
            return self

        def get(self):
            return None

    class _Controller:
        def post_screencap(self):
            return _Shot()

    class _Tasker:
        controller = _Controller()

    class _Ctx:
        tasker = _Tasker()

    original = store._ocr

    def fake_ocr(_context, roi, _image=None):
        top = roi[1]
        col = roi[0] + roi[2] // 2
        if top == layout["pages"][0]["name_y"] - 18:
            return [("数据协议", [roi[0], roi[1], roi[2], roi[3]])]
        if top == layout["pages"][0]["price_y"] - 22:
            return [("10" if col == 100 else "30", [roi[0], roi[1], roi[2], roi[3]])]
        return [("剩余 1", [roi[0], roi[1], roi[2], roi[3]])]

    store._ocr = fake_ocr
    try:
        shelf = store._scan_page(_Ctx(), layout)
    finally:
        store._ocr = original

    assert len(shelf) == 2
    assert [(e["name"], e["price"]) for e in shelf] == [("数据协议", 10), ("数据协议", 30)]


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
    for test in tests:
        test()
    print(f"STORE_PURCHASE_OK ({len(tests)} tests)")


if __name__ == "__main__":
    main()
