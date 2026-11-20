# -*- coding: utf-8 -*-
"""订单好友 Agent（Custom 动作）

本地状态 config/orders_state.json（长期保留，拉黑不删条目）：
  {
    "friends": [
      { "uid": "123", "type": "8-1", "status": "held" },      # 已加、还占着
      { "uid": "456", "type": "6-1", "status": "released" }   # 已拉黑，记录仍在
    ]
  }

每人 status：
  held     → 还占着（加了未拉黑）
  released → 已拉黑（记录保留；该 type 可再接新单）

流程（独立任务「订单」）：
  订单_前置_添加订单群好友 → 未配置 UID/链接 → 跳过加好友，直接进 订单_加好友后衔接
  取下一单     → 面板填 UID 则直接登记；否则网站取 type 尚无 held 的单；没有 →「加好友完毕」
  加好友完毕   →「订单_加好友后衔接」→ 订单交付/订单库
  输入当前UID  → 输入最后一条 held 的 uid
  订单_后置_拉黑订单群好友 → 没有 held → 直接回首页
  取下一待删   → 第一条 held 绑 OCR；没有 held →「清理完毕退出」
  删除登记完成 → 该条改为 released（不删列表）

配置来源（优先级从高到低）：
  1. GUI 面板输入框（「订单」任务的 订单好友_好友ID，存实例配置 TaskItems[订单].option[].data）
  2. 节点 param.url
  3. config/orders_source.json 的 url
"""

import json
import re
import urllib.request
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "config" / "orders_state.json"
SOURCE_PATH = ROOT / "config" / "orders_source.json"

# GUI 面板里的任务名与输入项名（须与 interface.json 保持一致）
TASK_NAME = "订单"
OPT_FRIEND = "订单好友_好友ID"
UID_RE = re.compile(r"\d{4,20}")
URL_RE = re.compile(r"https?://")


# Android KEYCODE_* — MuMu/ADB 上 Ctrl+A 常无效或映射错误，改用退格清空。
KEYCODE_MOVE_END = 123
KEYCODE_DEL = 67
SEARCH_FIELD_CLEAR_DEL_COUNT = 16


def _clear_search_field(ctrl):
    """Clear the focused search box without Ctrl+A (unreliable on emulators)."""
    ctrl.post_click_key(KEYCODE_MOVE_END).wait()
    for _ in range(SEARCH_FIELD_CLEAR_DEL_COUNT):
        ctrl.post_click_key(KEYCODE_DEL).wait()


def _norm_friends(raw):
    """统一每条带 status；旧数据没有 status 的一律当 held。"""
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid") or "").strip()
        typ = str(item.get("type") or "").strip()
        if not typ:
            continue
        st = item.get("status")
        if st not in ("held", "released"):
            st = "held"
        out.append({"uid": uid, "type": typ, "status": st})
    return out


def load_state():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {"friends": _norm_friends(data.get("friends"))}
    except Exception:
        return {"friends": []}


def save_state(state):
    """只写 friends；旧文件里的 phase 等多余字段不再保留。"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {"friends": _norm_friends(state.get("friends"))},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def orders_url(param):
    url = str((param or {}).get("url") or "").strip()
    if url:
        return url
    try:
        return str(json.loads(SOURCE_PATH.read_text(encoding="utf-8")).get("url") or "").strip()
    except Exception:
        return ""


def _walk_options(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        yield from _walk_options(item.get("option") or item.get("sub_options"))


def panel_friend_input(task_name=TASK_NAME, option_name=OPT_FRIEND):
    """读实例配置里指定任务的输入框；未填/读不到返回空串。"""
    try:
        from chip_filter_flow import instance_config_path

        data = json.loads(instance_config_path().read_text(encoding="utf-8-sig"))
    except Exception:
        return ""
    item = next((i for i in data.get("TaskItems", []) if i.get("name") == task_name), None)
    if not item:
        return ""
    for option in _walk_options(item.get("option")):
        if option.get("name") != option_name:
            continue
        for value in (option.get("data") or {}).values():
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def order_config(param=None, task_name=TASK_NAME, option_name=OPT_FRIEND):
    """返回 ("uid", 纯数字) / ("url", 链接) / ("", "")。面板填写优先。"""
    raw = panel_friend_input(task_name, option_name)
    if raw:
        if UID_RE.fullmatch(raw):
            return "uid", raw
        if URL_RE.match(raw):
            return "url", raw
        tip(f"订单好友：面板填写的「{raw}」既不是纯数字 UID 也不是 http 链接，按未配置处理。")
        return "", ""
    url = orders_url(param)
    return ("url", url) if url else ("", "")


def fetch_page(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LAA-order-fetch/1.0)"},
    )
    return urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace")


def fetch_uid_type_list(url):
    """Return parsed (uid, type) rows. May be empty if page has no lines.

    Raises only on network/HTTP failure. Content-warning / empty body returns []
    so callers can treat it as「没有可加的单」and continue to 基建.
    """
    html = fetch_page(url)
    if re.search(
        r"content warnings|Do you wish to continue\?",
        html,
        flags=re.I,
    ) and not re.search(r"\d{6,}\s*\|\s*\S+", html):
        tip(
            "订单页带内容警告（Content Warning），正文未展开，读不到 uid|类型；"
            "将跳过添加好友，继续基建。若要加好友，请去掉 rentry 警告或换可直读的订单页。"
        )
        return []

    m = re.search(r'<div class="entry-text"[\s\S]*?<p>([\s\S]*?)</p>', html)
    text = m.group(1) if m else html
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        uid, typ = [x.strip() for x in line.split("|", 1)]
        if uid.isdigit() and typ:
            rows.append((uid, typ))
    if not rows:
        for uid, typ in re.findall(r"(\d{6,})\s*\|\s*([^\s|<]+)", html):
            rows.append((uid, typ.strip()))
    return rows


def held_types(friends):
    return {f["type"] for f in friends if f.get("status") == "held"}


def held_friends(friends):
    return [f for f in friends if f.get("status") == "held" and f.get("uid")]


def tip(msg: str) -> None:
    """Print a user-visible tip; MFA surfaces Agent stdout in the run log."""
    print(f"[提示] {msg}", flush=True)


def order_source_ready():
    """Only require config file + url. Empty/unreadable order pages still allow 基建."""
    if not SOURCE_PATH.is_file():
        return (
            False,
            "缺少订单源配置文件 config/orders_source.json。"
            "请复制 config/orders_source.example.json 为 orders_source.json，并填写订单页 url。",
        )
    url = orders_url({})
    if not url:
        return (
            False,
            "config/orders_source.json 里没有有效的 url 字段，无法使用「添加订单群好友」。",
        )
    try:
        fetch_page(url)
    except Exception as e:
        return False, f"无法访问订单页（{e}）。url={url}"
    return True, url


@AgentServer.custom_action("订单_检查好友配置")
class OrderGroupCheckConfig(CustomAction):
    """订单任务加好友前置：填了 UID 就只加这一位；否则用订单页链接；都没配则跳过加好友继续交付。"""

    def run(self, context, argv):
        mode, value = order_config()
        if not mode:
            tip("订单：未配置好友 UID / 订单页链接，跳过加好友，继续订单交付。")
            context.override_next(argv.node_name, ["订单_加好友后衔接"])
            return True
        if mode == "uid":
            state = load_state()
            if any(f.get("uid") == value for f in state["friends"]):
                tip(f"订单：UID {value} 已处理过，跳过加好友，继续订单交付。")
                context.override_next(argv.node_name, ["订单_加好友后衔接"])
                return True
            tip(f"订单：使用面板 UID {value}，只添加这一位。")
            return True
        ok, detail = order_source_ready()
        if not ok:
            tip(f"订单·添加订单群好友失败：{detail}")
            tip("跳过加好友，继续订单交付；修好配置后重新勾选再试。")
            context.override_next(argv.node_name, ["订单_加好友后衔接"])
            return True
        tip(f"订单：使用订单页链接 {detail}，开始加好友。")
        return True


@AgentServer.custom_action("订单好友_拉黑检查")
class OrderFriendBlacklistCheck(CustomAction):
    """加好友结束后进入拉黑阶段；没有待拉黑的人就直接回首页。"""

    def run(self, context, argv):
        pending = held_friends(load_state()["friends"])
        if not pending:
            tip("订单好友：没有需要拉黑的好友，跳过拉黑。")
            context.override_next(argv.node_name, ["子任务_进入首页"])
            return True
        tip(f"订单好友：开始拉黑，剩余 {len(pending)} 人。")
        return True


@AgentServer.custom_action("取下一单")
class TakeNextOrder(CustomAction):
    def run(self, context, argv):
        try:
            param = json.loads(argv.custom_action_param or "{}")
        except json.JSONDecodeError:
            param = {}

        mode, value = order_config(param)
        if not mode:
            tip("订单好友：未配置好友 ID / 订单链接，跳过添加。")
            context.override_next(argv.node_name, ["加好友完毕"])
            return True

        state = load_state()
        friends = state["friends"]

        if mode == "uid":
            if any(f.get("uid") == value for f in friends):
                tip(f"订单好友：UID {value} 已处理过，跳过添加。")
                context.override_next(argv.node_name, ["加好友完毕"])
                return True
            friends.append({"uid": value, "type": "manual", "status": "held"})
            state["friends"] = friends
            save_state(state)
            tip(f"订单好友：已登记面板 UID {value}，准备搜索添加。")
            return True

        used = held_types(friends)
        print(f"[取下一单] friends={friends} held_types={sorted(used)}", flush=True)

        try:
            orders = fetch_uid_type_list(value)
        except Exception as e:
            tip(f"订单好友：拉取订单页失败（{e}），跳过添加。")
            save_state(state)
            context.override_next(argv.node_name, ["加好友完毕"])
            return True
        print(f"[取下一单] orders={orders}", flush=True)

        pick = next(((u, t) for u, t in orders if t not in used), None)
        if not pick:
            tip("订单好友：当前没有可加的新订单类型，跳过添加。")
            save_state(state)
            context.override_next(argv.node_name, ["加好友完毕"])
            return True

        uid, typ = pick
        friends.append({"uid": uid, "type": typ, "status": "held"})
        state["friends"] = friends
        save_state(state)
        tip(f"订单好友：已取到 {uid}|{typ}，准备搜索添加。")
        return True


@AgentServer.custom_action("输入当前UID")
class InputCurrentUid(CustomAction):
    def run(self, context, argv):
        held = held_friends(load_state()["friends"])
        if not held:
            tip("订单好友：本地没有待添加的好友记录，跳过添加。")
            context.override_next(argv.node_name, ["加好友完毕"])
            return True
        uid = str(held[-1]["uid"])

        ctrl = context.tasker.controller
        _clear_search_field(ctrl)

        tip(f"订单好友：正在输入 UID {uid}")
        ctrl.post_input_text(uid).wait()
        return True


@AgentServer.custom_action("取下一待删好友")
class TakeNextFriendToDelete(CustomAction):
    def run(self, context, argv):
        state = load_state()
        pending = held_friends(state["friends"])

        if not pending:
            tip("拉黑订单群好友：没有需要拉黑的 held 好友，清理结束。")
            save_state(state)
            context.override_next(argv.node_name, ["清理完毕退出"])
            return True

        target = pending[0]
        uid = target["uid"]
        save_state(state)
        context.override_pipeline(
            {
                "找到指定好友了": {
                    "recognition": {"type": "OCR", "param": {"expected": [uid]}},
                }
            }
        )
        tip(
            f"拉黑订单群好友：开始处理 {uid}|{target.get('type')} "
            f"（剩余 held {len(pending)} 人）"
        )
        return True


@AgentServer.custom_action("删除登记完成")
class FinishDeleteClaim(CustomAction):
    """拉黑成功：把第一条 held 标成 released，条目保留。"""

    def run(self, context, argv):
        state = load_state()
        friends = state["friends"]
        for f in friends:
            if f.get("status") == "held" and f.get("uid"):
                f["status"] = "released"
                tip(
                    f"拉黑订单群好友：{f['uid']}|{f.get('type')} 已登记为 released"
                    f"（仍 held={len(held_friends(friends))}）"
                )
                break
        else:
            tip("拉黑订单群好友：没有可登记的 held 记录（可能已被清理）。")

        state["friends"] = friends
        save_state(state)
        remaining = len(held_friends(friends))
        print(f"[删除登记完成] 仍 held={remaining}", flush=True)
        return True
