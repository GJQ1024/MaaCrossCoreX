#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""参数化账号切换 / 登录编排（识别定位，不录回放、不写死坐标）。

流程（每一步都靠 OCR 文案或模板匹配定位后再点）：
  标题页「开始游戏」→ 大厅 → 右上角方块菜单图标（模板）→ 玩家面板「设置」
  → 设置页「用户中心」→ 账号管理「切换账号」→「您确认要切换账号吗」点「确认」
  → SDK「切换其他账号」→ 手机号页（协议勾选模板 + 粉色按键模板）
  → 账号密码登录（OCR 占位文字定位输入框）→「登录」→ 校验登录成功
  → 标题页 →「开始游戏」→ 大厅。

安全规则：
  * 「账号注销 / 解除授权 / 删除用户数据」类弹窗只点「取消 / 返回 / 关闭」或系统返回键，
    绝不点「确认」；
  * 出现实名认证、验证码、风控提示立刻停手（退出码 3），不做任何绕行。

幂等：
  大厅读 UID，账号管理面板读 bili_ 号；与目标（--expect-uid / --expect-nick，
  或本地状态文件 <MAA>/sandbox/account_switch_state.json 里该序号的记录）一致时直接返回成功。

凭据：
  只从 --accounts（默认 <MAA>/sandbox/accounts.txt）按序号读取；不接受命令行明文密码。
  日志、结构化输出都不出现账号 / 密码原文，账号只写序号。

退出码：
  0 成功（含“当前已是目标账号，跳过”）
  1 流程失败（超时 / 界面无法判定 / 登录报错）
  2 环境或参数错误（设备不是 V2284A、清单序号越界等）
  3 需要人工介入（实名认证 / 验证码 / 风控）

用法：
  python scripts/account_switch.py --index 2
  python scripts/account_switch.py --index 1 --expect-uid 104445339
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

# 路径全部从本文件位置反推，不假定当前工作目录：
#   PROJECT_ROOT = MCC_Framework 根（本脚本在 <根>/scripts/ 下）
#   MAA_ROOT     = 上层工作区根（账号清单与状态文件放在 <MAA>/sandbox/）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAA_ROOT = PROJECT_ROOT.parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "agent"))

import pretask_landscape as device  # noqa: E402  仓库内唯一的 adb 路径 / serial 解析实现
import device_judge  # noqa: E402  待机画面判据：游戏屏保 vs Android 系统锁屏
import tutorial_state  # noqa: E402  当前账号缓存（tutorial_once 账号维度）

ADB_PATH = device.adb_path()
SERIAL = device.detect_serial() or ""
RESOURCE_DIR = PROJECT_ROOT / "assets" / "resource"

os.environ.setdefault(
    "MAAFW_BINARY_PATH", str(MAA_ROOT / "sandbox" / "MaaFramework-5.14.3" / "bin")
)

from maa.controller import AdbController  # noqa: E402
from maa.resource import Resource  # noqa: E402
from maa.tasker import Tasker  # noqa: E402


def bind_tasker(serial: str):
    """绑定仓库资源与指定实例的 AdbController。"""
    res = Resource()
    if not res.post_bundle(str(RESOURCE_DIR)).wait().succeeded:
        print("[acct] FAIL: resource 加载失败")
        sys.exit(1)
    ctrl = AdbController(adb_path=ADB_PATH, address=serial)
    if not ctrl.post_connection().wait().succeeded:
        print(f"[acct] FAIL: adb 连接失败 {serial}")
        sys.exit(1)
    tasker = Tasker()
    if not tasker.bind(resource=res, controller=ctrl):
        print("[acct] FAIL: tasker 绑定失败")
        sys.exit(1)
    if not tasker.inited:
        print("[acct] FAIL: tasker 未初始化")
        sys.exit(1)
    return tasker

TARGET_W, TARGET_H = 1280, 720
GAME_PACKAGE = "com.megagame.crosscore.bilibili"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
UID_RE = re.compile(r"UID[：:\s]*(\d+)")
BILI_RE = re.compile(r"bili_\d+")
SAFE_TEXT_RE = re.compile(r"[A-Za-z0-9@._+-]+")

ACCOUNTS_DEFAULT = MAA_ROOT / "sandbox" / "accounts.txt"
STATE_FILE_DEFAULT = MAA_ROOT / "sandbox" / "account_switch_state.json"


def refresh_account_cache(uid, index, source: str = "account_switch") -> None:
    """刷新 tutorial_once 的当前账号缓存；uid 为空表示未知，闸会 fail-safe 跳过。"""
    try:
        tutorial_state.write_account_cache(uid, index, source=source)
    except OSError as exc:
        print(f"[acct] 账号缓存写入失败（不影响切号结果）：{exc}")

# 同一种界面重复出现的上限，超过即判定卡死（防止无脑循环）
STATE_LIMITS = {
    "privacy": 2,
    "notice": 4,
    "title": 3,
    "lobby": 4,
    "player_panel": 3,
    "settings": 3,
    "account_panel": 3,
    "switch_confirm": 3,
    "sdk_record": 3,
    "phone_login": 4,
    "agree_toast": 3,
    "pwd_form": 2,
}


def now_clock() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def redact(text: str) -> str:
    """日志用：邮箱等敏感串打码。"""
    return EMAIL_RE.sub("***@***", text)


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def adb(serial: str, args: list[str], binary: bool = False, timeout: int = 30):
    cmd = [ADB_PATH, "-s", serial, *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"adb 调用异常（{args[0]}）：{exc}") from exc
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"adb 返回非零（{args[0]}）：{err}")
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def _rect(box):
    """兼容 box 的两种返回形态：Rect(.x/.y/.w/.h) 与 list[x, y, w, h]。"""
    if hasattr(box, "x"):
        return box.x, box.y, box.w, box.h
    x, y, w, h = (int(v) for v in box)
    return x, y, w, h


def find_exact(items, text: str):
    target = norm(text)
    for item in items:
        if norm(item.text) == target:
            return item
    return None


def find_contains(items, *keywords: str):
    for item in items:
        value = norm(item.text)
        if any(k in value for k in keywords):
            return item
    return None


def load_account(path: Path, index: int) -> dict:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rows.append(line.split("\t"))
    if index < 1 or index > len(rows):
        raise ValueError(f"账号序号 {index} 越界（清单共 {len(rows)} 个）")
    row = rows[index - 1]
    if len(row) < 2 or not row[0].strip() or not row[1].strip():
        raise ValueError(f"清单第 {index} 行缺少账号或密码列")
    return {"account": row[0].strip(), "password": row[1].strip()}


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


class SwitchRun:
    def __init__(self, args, cred: dict, state: dict):
        self.args = args
        self.cred = cred
        self.state_data = state
        self.serial = args.serial
        self.out_dir = Path(args.out).resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        self.log_lines: list[str] = []
        self.last_frame: Path | None = None
        self.started = time.monotonic()
        self.hits: dict[str, int] = {}
        self.unknown = 0
        self.restarts = 0
        self.wake_tries = 0
        self.filled = False
        self.bili: str | None = None
        self.uid_seen: str | None = None
        # 是否已点过「确认切换账号」：只有过了这一步，当前登录号才真正变成未知。
        # 失败分支据此决定要不要把 account_current.json 置空（见 main）。
        self.switch_started = False
        self.dry_run = bool(getattr(args, "dry_run", False))

        self.tasker = bind_tasker(self.serial)
        # 只取一次 controller 包装对象：重复访问会生成多个 Python 包装，
        # 各自析构时可能销毁同一个底层句柄。
        self.ctrl = self.tasker.controller

    # ---------- 基础能力 ----------

    def log(self, message: str, frame: Path | None = None) -> None:
        line = f"{now_clock()} {message}"
        if frame is not None:
            line += f" | {frame.name}"
        self.log_lines.append(redact(line))
        print(f"[acct] {redact(line)}", flush=True)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def grab(self, label: str) -> tuple[np.ndarray, Path]:
        data = adb(self.serial, ["exec-out", "screencap", "-p"], binary=True)
        image = Image.open(io.BytesIO(data))
        if image.size != (TARGET_W, TARGET_H):
            raise RuntimeError(f"截图尺寸 {image.size}，期望 1280x720")
        rgb = image.convert("RGB")
        path = self.out_dir / f"{self.seq:02d}_{label}.png"
        self.seq += 1
        path.write_bytes(data)
        self.last_frame = path
        return np.asarray(rgb)[:, :, ::-1].copy(), path

    def ocr(self, image: np.ndarray) -> list:
        height, width = image.shape[:2]
        job = self.tasker.post_task("屏幕扫描", {
            "屏幕扫描": {
                "recognition": "OCR",
                "roi": [0, 0, width, height],
                "action": "DoNothing",
            }
        })
        job.wait()
        items: list = []
        detail = self.tasker.get_task_detail(job.job_id)
        for node_id in getattr(detail, "node_id_list", []) or []:
            node = self.tasker.get_node_detail(node_id)
            reco = getattr(node, "recognition", None) if node else None
            for item in getattr(reco, "all_results", []) or []:
                items.append(item)
        return items

    def match(self, template: str, threshold: float = 0.85, roi=None,
              order_by: str = "Score"):
        """模板匹配，返回命中结果（含 box/score）或 None。

        order_by 默认 Score：MAA 默认 Horizontal 会让 best_result 取到最左侧的
        低分误命中（大厅图标实测 0.5484@[93,546] 压过真图标 1.0000@[1199,26]）。
        """
        job = self.tasker.post_task("模板匹配", {
            "模板匹配": {
                "recognition": "TemplateMatch",
                "template": [template],
                "roi": roi or [0, 0, TARGET_W, TARGET_H],
                "threshold": threshold,
                "order_by": order_by,
                "action": "DoNothing",
            }
        })
        job.wait()
        best = None
        detail = self.tasker.get_task_detail(job.job_id)
        for node_id in getattr(detail, "node_id_list", []) or []:
            node = self.tasker.get_node_detail(node_id)
            reco = getattr(node, "recognition", None) if node else None
            if reco is None or not getattr(reco, "hit", False):
                continue
            result = getattr(reco, "best_result", None)
            if result is not None:
                best = result
        return best

    def click_box(self, box, label: str) -> None:
        bx, by, bw, bh = _rect(box)
        x = int(bx + bw / 2)
        y = int(by + bh / 2)
        self.ctrl.post_click(x, y).wait()
        self.log(f"点击 {label} ({x},{y})")
        time.sleep(self.args.click_delay)

    def type_text(self, text: str, label: str) -> None:
        if not SAFE_TEXT_RE.fullmatch(text):
            raise RuntimeError(f"{label}包含不受支持的字符，拒绝输入")
        adb(self.serial, ["shell", "input", "text", text], timeout=20)
        self.log(f"已输入{label}（内容不记录）")
        time.sleep(0.4)

    def hide_keyboard(self) -> None:
        try:
            shown = adb(self.serial, ["shell", "dumpsys", "input_method"], timeout=15)
        except Exception:  # noqa: BLE001
            shown = ""
        if "mInputShown=true" in shown:
            self.ctrl.post_click_key(4).wait()
            time.sleep(0.6)
            self.log("已按返回键收起软键盘")

    def wake_screen(self, kind: str, frame: Path, reason: str) -> None:
        """按状态分流唤醒：游戏屏保轻点/BACK，系统锁屏才用唤醒键+上滑。

        唤醒结果不在本方法内断言——下一轮重新截图 + 重新诊断才算复核；
        只有前台离开待机画面（kind=normal）才记「复核通过」，停在弹窗则交给
        既有的界面状态机处理。
        """
        label = device_judge.SCREEN_LABELS[kind]
        if kind == "system_lock":
            self.log(f"检测到{label}（{reason}），"
                     f"发 KEYCODE_WAKEUP({device_judge.WAKE_KEYEVENT}) + 上滑解锁", frame)
            device_judge.unlock_system(self.serial, ADB_PATH)
            time.sleep(1.2)
            return

        if self.wake_tries == 1:
            point = device_judge.tap_neutral(self.serial, ADB_PATH)
            self.log(f"检测到{label}（{reason}），轻点中性点 {point} 唤醒，下一帧复核", frame)
        elif self.wake_tries == 2:
            device_judge.press_back(self.serial, ADB_PATH)
            self.log(f"轻点后仍是{label}，改按 BACK({device_judge.BACK_KEYEVENT})，下一帧复核", frame)
        else:
            self.log(f"轻点与 BACK 均未唤醒{label}，降级发 KEYCODE_WAKEUP 复核", frame)
            device_judge.unlock_system(self.serial, ADB_PATH)
        time.sleep(1.2)

    def restart_to_title(self) -> None:
        self.restarts += 1
        self.log(f"界面无法判定，重启游戏（第 {self.restarts} 次）")
        adb(self.serial, ["shell", "am", "force-stop", GAME_PACKAGE], timeout=20)
        time.sleep(2)
        adb(self.serial, ["shell", "monkey", "-p", GAME_PACKAGE,
                          "-c", "android.intent.category.LAUNCHER", "1"], timeout=20)
        time.sleep(20)
        self.unknown = 0
        self.hits.clear()

    # ---------- 界面判定 ----------

    @staticmethod
    def classify(items) -> str:
        joined = "".join(norm(item.text) for item in items)

        unsafe = ("系统提醒" in joined or "您确认" in joined) and any(
            key in joined for key in ("注销", "解除授权", "删除用户数据")
        )
        if unsafe:
            return "unsafe_dialog"
        if any(key in joined for key in ("实名认证", "提交实名", "请填写真实姓名", "有效身份证号")):
            return "blocked"
        if any(key in joined for key in ("验证码", "人机验证", "安全验证", "滑块验证",
                                         "操作频繁", "风险验证", "账号异常")):
            return "blocked"
        if any(key in joined for key in ("密码错误", "账号或密码错误", "登录失败")):
            return "error"
        if "隐私政策提示" in joined:
            return "privacy"
        if "今天不再提示" in joined:
            return "notice"
        if "账号密码登录" in joined and "请输入密码" in joined:
            return "pwd_form"
        if "您还未同意我们的条款" in joined:
            return "agree_toast"
        if "您确认要切换账号吗" in joined:
            return "switch_confirm"
        if "切换其他账号" in joined:
            return "sdk_record"
        if "账号管理" in joined and "切换账号" in joined:
            return "account_panel"
        if "请输入手机号" in joined:
            return "phone_login"
        if "开始游戏" in joined:
            return "title"
        if "用户中心" in joined and "画面设置" in joined:
            return "settings"
        # 配置面板（右侧抽屉）除「设置」外还带 成就/档案/教程/好友/宿舍/战术/公告。
        # 旧判据要求同时命中「教程」「好友」，OCR 漏一个就会被误判回 lobby，
        # 随后在面板遮挡下再找右上角图标 → 误报 lobby_menu_missing（2026-11-08 实测）。
        # 改为「设置 + 任两项」：大厅主界面不含这些词，不会误判。
        if "设置" in joined and sum(
            key in joined for key in ("成就", "档案", "教程", "好友", "宿舍", "战术", "公告")
        ) >= 2:
            return "player_panel"
        if "UID:" in joined or "UID：" in joined:
            return "lobby"
        return "unknown"

    @staticmethod
    def extract_uid(items) -> str | None:
        for item in items:
            matched = UID_RE.search(item.text)
            if matched:
                return matched.group(1)
        return None

    @staticmethod
    def extract_nick(items, uid_item) -> str | None:
        if uid_item is None:
            return None
        _, uid_y, _, _ = _rect(uid_item.box)
        candidates = [
            item for item in items
            if _rect(item.box)[1] < uid_y
            and _rect(item.box)[0] < 200
            and 2 <= len(item.text.strip()) <= 20
            and not re.fullmatch(r"[0-9:]+", item.text.strip())
        ]
        return candidates[0].text.strip() if candidates else None

    def target_uid(self) -> str | None:
        if self.args.expect_uid:
            return str(self.args.expect_uid)
        record = self.state_data.get(str(self.args.index)) or {}
        return record.get("uid")

    def target_bili(self) -> str | None:
        record = self.state_data.get(str(self.args.index)) or {}
        return record.get("bili")

    def is_target_account(self, uid: str | None, nick: str | None) -> bool:
        if self.args.expect_nick and nick and norm(nick) == norm(self.args.expect_nick):
            return True
        expected = self.target_uid()
        return bool(expected and uid and str(uid) == str(expected))

    def persist(self, uid: str | None, nick: str | None, bili: str | None) -> None:
        record = self.state_data.get(str(self.args.index)) or {}
        if uid:
            record["uid"] = str(uid)
        if nick:
            record["nickname"] = nick
        if bili:
            record["bili"] = bili
        record["updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        self.state_data[str(self.args.index)] = record
        try:
            STATE_FILE_DEFAULT.write_text(
                json.dumps(self.state_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.log(f"状态文件写入失败（不影响本次结果）：{exc}")

    # ---------- 动作 ----------

    def handle_unsafe_dialog(self, items) -> None:
        """注销 / 解除授权 / 删除数据类弹窗：只取消，绝不确认。"""
        button = find_exact(items, "取消") or find_exact(items, "返回") or find_exact(items, "关闭")
        if button is not None:
            self.click_box(button.box, "危险弹窗-取消")
            self.log("已拦截危险弹窗（注销/解授权/删数据），只点取消")
        else:
            self.ctrl.post_click_key(4).wait()
            time.sleep(0.8)
            self.log("已拦截危险弹窗，按系统返回键退出（未点确认）")

    def fill_credentials(self, items) -> None:
        account_field = find_contains(items, "请输入账号")
        if account_field is None:
            raise RuntimeError("账号密码页未识别到账号输入框")
        self.click_box(account_field.box, "账号输入框")
        self.type_text(self.cred["account"], "账号")
        self.hide_keyboard()

        image, _ = self.grab("after_account")
        items2 = self.ocr(image)
        password_field = find_contains(items2, "请输入密码")
        if password_field is None:
            raise RuntimeError("账号密码页未识别到密码输入框")
        self.click_box(password_field.box, "密码输入框")
        self.type_text(self.cred["password"], "密码")
        self.hide_keyboard()
        self.filled = True
        self.log("账号密码已填入（内容不记录）")

    def agreement_roi(self, items):
        text_item = find_contains(items, "已阅读并同意")
        if text_item is None:
            return None
        bx, by, _, bh = _rect(text_item.box)
        return [max(0, bx - 70), max(0, by - 30), 140, bh + 60]

    # ---------- 结果输出 ----------

    def emit(self, payload: dict) -> None:
        (self.out_dir / "run.log").write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
        payload["frames"] = str(self.out_dir)
        payload["elapsed_s"] = round(self.elapsed(), 1)
        (self.out_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("RESULT " + json.dumps(payload, ensure_ascii=False), flush=True)

    def success(self, already: bool, uid, nick, bili) -> int:
        if uid:
            self.persist(uid, nick, bili)
        # 切号后立刻刷新当前账号缓存，避免 tutorial_once 沿用上一个号的「已过新手」状态。
        refresh_account_cache(uid, self.args.index)
        self.log(f"完成：{'当前已是目标账号，跳过' if already else '已完成切换/登录'}"
                 f" uid={uid} nick={redact(nick or '')} bili={bili}")
        self.emit({
            "ok": True, "already": already, "account_index": self.args.index,
            "uid": uid, "nickname": nick, "bili": bili, "serial": self.serial,
        })
        return 0

    def fail(self, reason: str, detail: str, code: int) -> int:
        self.log(f"失败：{reason} | {detail}")
        self.emit({
            "ok": False, "reason": reason, "detail": detail,
            "account_index": self.args.index, "serial": self.serial,
            "last_frame": str(self.last_frame) if self.last_frame else None,
        })
        return code

    # ---------- 主循环 ----------

    def run(self) -> int:
        try:
            return self._run()
        except Exception as exc:  # noqa: BLE001
            return self.fail("exception", f"{type(exc).__name__}: {exc}", 1)

    def _run(self) -> int:
        self.log(f"开始切号：acc#{self.args.index}，设备 {self.serial}，输出 {self.out_dir}")
        while True:
            if self.elapsed() > self.args.timeout:
                return self.fail("timeout", f"超过 {self.args.timeout} 秒未完成", 1)

            image, frame = self.grab("scan")
            items = self.ocr(image)
            screen = device_judge.diagnose_screen(items, self.serial, ADB_PATH)
            if screen["kind"] in ("game_screensaver", "idle_unknown", "system_lock"):
                self.wake_tries += 1
                if self.wake_tries > 6:
                    return self.fail(
                        "screen_stuck",
                        f"{device_judge.SCREEN_LABELS[screen['kind']]}连续 6 帧未唤醒",
                        1,
                    )
                self.wake_screen(screen["kind"], frame, screen["reason"])
                continue
            woke = self.wake_tries
            self.wake_tries = 0
            state = self.classify(items)
            target_name = f"{int(frame.name.split('_')[0]):02d}_{state}.png"
            renamed = frame.with_name(target_name)
            if renamed != frame:
                frame.rename(renamed)
                frame = renamed
            self.last_frame = frame
            if woke:
                self.log(f"唤醒复核通过：已离开待机画面（{woke} 次动作），"
                         f"当前界面 {state}", frame)
            self.log(f"状态 {state}（{len(items)} 条 OCR）", frame)

            if state == "unknown":
                self.unknown += 1
                if self.unknown >= 10:
                    if self.restarts < self.args.restart_retries:
                        self.restart_to_title()
                        continue
                    return self.fail("unknown_state", "连续 10 帧无法识别当前界面", 1)
                time.sleep(1.0)
                continue

            self.unknown = 0
            self.hits[state] = self.hits.get(state, 0) + 1
            limit = STATE_LIMITS.get(state)
            if limit and self.hits[state] > limit:
                return self.fail("stuck", f"界面 {state} 重复 {self.hits[state]} 次仍无进展", 1)

            if state == "unsafe_dialog":
                self.handle_unsafe_dialog(items)
                continue

            if state == "blocked":
                return self.fail(
                    "manual_required",
                    "出现实名认证 / 验证码 / 风控提示，按红线停手，未做任何绕过",
                    3,
                )

            if state == "error":
                return self.fail("login_error", "登录页提示账号或密码错误", 1)

            if state == "privacy":
                button = find_exact(items, "同意")
                if button is None:
                    return self.fail("privacy_button_missing", "隐私弹窗未识别到「同意」", 1)
                self.click_box(button.box, "隐私政策-同意")
                continue

            if state == "notice":
                # 实机验证：真正关闭公告的是右上角 X，「今天不再提示」按钮常常无效。
                # 顺序：X 模板 → 文案兜底；两者都没有才停手。
                close = self.match(
                    "StartUp/CloseAnnouncement.png", 0.85, roi=[900, 55, 362, 200]
                )
                if close is not None:
                    self.click_box(close.box, "公告-右上角X")
                    continue
                button = find_contains(items, "今天不再提示")
                if button is not None:
                    self.click_box(button.box, "公告-今天不再提示（兜底）")
                    continue
                return self.fail(
                    "notice_close_missing",
                    "公告弹窗既未匹配到右上角X，也未识别到「今天不再提示」",
                    1,
                )

            if state == "title":
                button = find_exact(items, "开始游戏")
                if button is None:
                    return self.fail("start_button_missing", "标题页未识别到「开始游戏」", 1)
                self.click_box(button.box, "开始游戏")
                time.sleep(2)
                continue

            if state == "lobby":
                uid_item = None
                for item in items:
                    if UID_RE.search(item.text):
                        uid_item = item
                        break
                uid = self.extract_uid(items)
                nick = self.extract_nick(items, uid_item)
                self.uid_seen = uid or self.uid_seen
                if self.is_target_account(uid, nick):
                    return self.success(True, uid, nick, self.bili)
                # 模板来源帧：apps/crosscore-auto/sandbox/tutorial_neg/28_clean_lobby/002.png
                # box [1199,26,46,36]（离线自检 1.0000；20_home 与 36_after_official 同为 1.0000）
                # ROI 锁右上角 + Score 排序，双保险避免取到左下的低分误命中
                # 2026-11-08 实测（RGB CCOEFF_NORMED，ROI [1150,0,130,90]）：
                #   大厅变体A（图标在 1199,26）0.997~0.999；变体B（1200,27，1px 偏移）0.846。
                #   阈值 0.85 卡在 0.846 之上，导致 B 类大厅误报 lobby_menu_missing（P-057）。
                #   负例：设置页 0.16 / 配置面板 0.36 / SDK 0.39，与正例最低 0.846 间隔 0.45。
                # 故降到 0.78：正例最低 0.846 留 0.066 余量，负例最高 0.39 留 0.39 余量。
                icon = self.match("acct_menu_grid.png", 0.78, roi=[1150, 0, 130, 90])
                if icon is None:
                    # 兜底：配置面板已打开时会盖住右上角图标，此时直接点面板里的「设置」。
                    fallback = find_exact(items, "设置")
                    if fallback is not None:
                        self.click_box(fallback.box, "设置（图标被面板遮挡的兜底）")
                        continue
                    return self.fail("lobby_menu_missing", "大厅未匹配到右上角菜单图标模板", 1)
                self.click_box(icon.box, "大厅菜单图标")
                time.sleep(1)
                continue

            if state == "player_panel":
                button = find_exact(items, "设置")
                if button is None:
                    return self.fail("settings_entry_missing", "玩家面板未识别到「设置」", 1)
                self.click_box(button.box, "设置")
                continue

            if state == "settings":
                button = find_exact(items, "用户中心")
                if button is None:
                    return self.fail("user_center_missing", "设置页未识别到「用户中心」", 1)
                self.click_box(button.box, "用户中心")
                continue

            if state == "account_panel":
                joined = "".join(item.text for item in items)
                matched = BILI_RE.search(joined)
                if matched:
                    self.bili = matched.group(0)
                if self.dry_run:
                    # 只读演练：到账号管理页即停，不点「切换账号」，不写账号缓存。
                    texts = [item.text for item in items if item.text.strip()]
                    self.log("dry-run：已到达账号管理页，停止（不切号）")
                    self.emit({
                        "ok": True, "dry_run": True, "account_index": self.args.index,
                        "uid": self.uid_seen, "bili": self.bili, "serial": self.serial,
                        "account_panel_texts": texts,
                    })
                    return 0
                if self.target_bili() and self.bili == self.target_bili():
                    return self.success(True, self.uid_seen, None, self.bili)
                button = find_exact(items, "切换账号")
                if button is None:
                    return self.fail("switch_row_missing", "账号管理面板未识别到「切换账号」", 1)
                self.click_box(button.box, "切换账号")
                continue

            if state == "switch_confirm":
                button = find_exact(items, "确认")
                if button is None:
                    return self.fail("confirm_missing", "切换确认弹窗未识别到「确认」", 1)
                self.click_box(button.box, "确认切换账号")
                self.switch_started = True
                time.sleep(2)
                continue

            if state == "sdk_record":
                button = find_exact(items, "切换其他账号")
                if button is None:
                    return self.fail("other_account_missing", "SDK 登录记录页未识别到「切换其他账号」", 1)
                self.click_box(button.box, "切换其他账号")
                time.sleep(2)
                continue

            if state in ("phone_login", "agree_toast"):
                roi = self.agreement_roi(items)
                checkbox = self.match("acct_agree_box.png", 0.85, roi=roi)
                if checkbox is not None:
                    self.click_box(checkbox.box, "条款勾选框")
                    time.sleep(1)
                    image, frame = self.grab("after_agree")
                    items = self.ocr(image)
                # 粉色键实测 box [610,431,60,60]（s07_phone_login.png，1.0000），
                # ROI 只框弹窗中部，避免全屏误命中
                pink = self.match("acct_pink_login.png", 0.85, roi=[560, 405, 200, 140])
                if pink is None:
                    return self.fail("pink_button_missing", "手机号登录页未匹配到粉色按键模板", 1)
                self.click_box(pink.box, "粉色按键")
                time.sleep(2)
                continue

            if state == "pwd_form":
                if not self.filled:
                    self.fill_credentials(items)
                    image, frame = self.grab("pwd_filled")
                    items = self.ocr(image)
                button = find_exact(items, "登录")
                if button is None:
                    return self.fail("login_button_missing", "账号密码页未识别到「登录」按钮", 1)
                self.click_box(button.box, "登录")
                time.sleep(3)
                continue

            return self.fail("unhandled_state", f"未处理状态 {state}", 1)


def resolve_plan(index, count: int, indices: str) -> list[int]:
    """把 --index / --count / --indices 解析成实际要切换的账号序号列表。

    优先级：--indices > --count > --index。三者都没给 -> 空列表（不切换）。
    """
    raw = [s for s in (indices or "").replace("，", ",").split(",") if s.strip()]
    if raw:
        return [int(s) for s in raw]
    if count and count > 0:
        start = index or 1
        return list(range(start, start + count))
    if index:
        return [index]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="交错战线账号切换/登录（识别定位）")
    ap.add_argument("--index", type=int, default=None, help="账号序号（按清单顺序，从 1 开始）")
    ap.add_argument("--count", type=int, default=0,
                    help="连续切换的账号数量（0=不切换；从 --index 起算，默认从 1 开始）")
    ap.add_argument("--indices", default="",
                    help="指定要切换的账号序号，逗号分隔（如 1,3,4）；优先于 --count")
    ap.add_argument("--plan", action="store_true", help="只打印解析出的切换顺序并退出（不执行切换）")
    ap.add_argument("--dry-run", action="store_true",
                    help="走到账号管理页即停：读取面板内容后退出，不点切换、不写账号缓存")
    ap.add_argument("--accounts", default=str(ACCOUNTS_DEFAULT), help="账号清单路径")
    ap.add_argument("--serial", default=SERIAL, help="adb 序列号（默认按包名校验解析）")
    ap.add_argument("--out", default=None, help="截图与日志输出目录")
    ap.add_argument("--expect-uid", default=None, help="目标账号 UID（幂等比对）")
    ap.add_argument("--expect-nick", default=None, help="目标账号昵称（幂等比对）")
    ap.add_argument("--timeout", type=float, default=240.0, help="整体超时（秒）")
    ap.add_argument("--restart-retries", type=int, default=1, help="界面无法判定时重启游戏的次数")
    ap.add_argument("--click-delay", type=float, default=0.8, help="点击后等待（秒）")
    args = ap.parse_args()

    try:
        plan = resolve_plan(args.index, args.count, args.indices)
    except ValueError as exc:
        print("RESULT " + json.dumps(
            {"ok": False, "reason": "args", "detail": f"--indices 解析失败：{exc}"},
            ensure_ascii=False))
        return 2

    if args.plan or not plan:
        detail = "不切换（未指定数量/账号）" if not plan else "仅打印计划"
        print("RESULT " + json.dumps(
            {"ok": True, "plan": plan, "detail": detail}, ensure_ascii=False))
        return 0

    if not args.serial:
        print("RESULT " + json.dumps(
            {"ok": False, "reason": "env", "detail": "未解析到唯一装了交错战线的实例，请显式 --serial"},
            ensure_ascii=False))
        return 2

    try:
        model = adb(args.serial, ["shell", "getprop", "ro.product.model"], timeout=15).strip()
    except Exception as exc:  # noqa: BLE001
        print("RESULT " + json.dumps(
            {"ok": False, "reason": "env", "detail": f"设备不可达：{exc}"}, ensure_ascii=False))
        return 2
    if "V2284A" not in model:
        print("RESULT " + json.dumps(
            {"ok": False, "reason": "wrong_instance",
             "detail": f"{args.serial} 型号 {model}，不是 JCZX(V2284A)，已拒绝操作"},
            ensure_ascii=False))
        return 2

    base_out = args.out
    if not base_out:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base_out = str(PROJECT_ROOT / "sandbox" / "shots" / "account_switch" / stamp)

    done = []
    for pos, idx in enumerate(plan, 1):
        try:
            cred = load_account(Path(args.accounts), idx)
        except Exception as exc:  # noqa: BLE001
            print("RESULT " + json.dumps(
                {"ok": False, "reason": "args", "detail": str(exc),
                 "done": done, "failed_index": idx},
                ensure_ascii=False))
            return 2
        args.index = idx
        args.out = base_out if len(plan) == 1 else str(Path(base_out) / f"acc{idx}")
        print(f"[account_switch] ({pos}/{len(plan)}) 切换到账号 {idx}", flush=True)
        runner = SwitchRun(args, cred, load_state(STATE_FILE_DEFAULT))
        rc = runner.run()
        done.append({"index": idx, "ok": rc == 0, "out": args.out})
        if rc != 0:
            # 失败即停：不带着异常状态继续切下一个号。
            # 只有已经点过「确认切换账号」时，当前登录号才真正未知，此时把缓存置空，
            # 让 tutorial_once fail-safe 跳过；若在确认之前失败（大厅/设置等导航问题），
            # 账号根本没变，保留原缓存，避免把已知 UID 误降级为 unknown。
            if runner.switch_started:
                refresh_account_cache(None, idx, source="switch_failed")
            else:
                print("[account_switch] 未点过确认，当前账号未变，保留原账号缓存", flush=True)
            print("RESULT " + json.dumps(
                {"ok": False, "reason": "switch_failed", "accounts": done,
                 "failed_index": idx}, ensure_ascii=False))
            return rc

    print("RESULT " + json.dumps({"ok": True, "accounts": done}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
