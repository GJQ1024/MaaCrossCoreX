"""关闭模拟器：只按目标 android_id 定位实例，不按实例号/枚举顺序挑。"""

from __future__ import annotations

import re

from maa.context import Context
from maa.custom_action import CustomAction

import ensure_mumu


# resolve_target() 的第三种结果：目标实例进程本来就不在，关闭目的已达成。
ALREADY_CLOSED = "already_closed"


def _process_alive(info):
    return bool(info.get("is_process_started") or info.get("is_android_started"))


def _running_instances(manager):
    """MuMuManager 里进程仍在运行的实例编号；读不到时返回空列表。"""
    return [
        index
        for index, info in ensure_mumu.manager_info_all(manager).items()
        if _process_alive(info)
    ]


def _cached_index():
    """上一次按 android_id 定位成功时写下的实例号（ensure_mumu 的 mumu_runtime.json）。

    只用于「已关」场景核对进程状态，绝不能拿它决定关哪台。
    """
    value = ensure_mumu.load_json(ensure_mumu.CACHE_FILE).get("vm_index")
    return int(value) if str(value).isdigit() else None


def _index_from_serial(serial):
    """按 MuMu 12 的端口约定 port = 16384 + 32 × index 反推实例号。

    只在 MuMuManager info 里读不到 adb_port（运行中的实例常缺该字段）时使用。
    这是约定推导、不是权威反查，所以调用方必须把这层推断打进日志。
    """
    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", serial or "")
    if not match:
        return None
    offset = int(match.group(1)) - 16384
    if offset < 0 or offset % 32:
        return None
    return offset // 32


def resolve_target():
    """按目标 android_id 定位要关闭的 MuMu 实例。

    链路：目标 android_id（device_identity 锚点）→ adb 在线设备 + 游戏包 → serial
    → MuMuManager info 反查 vmindex（权威）→ 读不到才按端口约定反推。

    返回值：
      (manager, index)   定位成功，可以关
      ALREADY_CLOSED     定位不到设备，但实例进程已不在：先用上次识别成功写下的
                         vmindex 查进程状态；拿不到 vmindex（实例已从 MuMuManager
                         消失）时按「没有任何实例在运行」判断 → 本来就没开
      None               实例进程还在、只是 adb 里没有目标设备 → 定位不到，拒绝关任何一台
    """
    _root, manager, adb = ensure_mumu.find_mumu()
    if not manager or not adb:
        print("[关闭模拟器] 未找到 MuMu 12 或 adb，停止（不自动挑）", flush=True)
        return None
    try:
        serial, android_id = ensure_mumu.resolve_adb_target(adb)
    except ensure_mumu.TargetResolutionError as exc:
        # 没有目标设备时拿不到 serial，只能退回「上次识别成功写下的实例号」查进程状态；
        # 这个编号只用来判断开没开，不用来决定关哪台。
        index = _cached_index()
        info = ensure_mumu.manager_info(manager, index) if index is not None else {}
        if info:
            if _process_alive(info):
                print(
                    f"[关闭模拟器] 无法按 android_id 唯一识别目标设备（{exc}）；"
                    f"上次记录的实例 {index} 进程仍在运行，说明只是 adb 里没有目标设备，"
                    "定位不到，停止（不随便关一台）",
                    flush=True,
                )
                return None
            print(
                f"[关闭模拟器] 无法按 android_id 唯一识别目标设备（{exc}）；"
                f"但上次记录的实例 {index} 进程已不在 → 判定模拟器本来就没开，"
                "关闭目的已达成",
                flush=True,
            )
            return ALREADY_CLOSED
        running = _running_instances(manager)
        if running:
            print(
                f"[关闭模拟器] 无法按 android_id 唯一识别目标设备（{exc}）；"
                f"拿不到上次的实例号，但实例 {running} 的进程仍在运行，"
                "定位不到目标，停止（不随便关一台）",
                flush=True,
            )
            return None
        print(
            f"[关闭模拟器] 无法按 android_id 唯一识别目标设备（{exc}）；"
            "且 MuMuManager 中没有任何实例进程在运行（或目标实例已从 MuMuManager 消失）"
            "→ 判定模拟器本来就没开，关闭目的已达成",
            flush=True,
        )
        return ALREADY_CLOSED

    index = ensure_mumu.index_for_serial(manager, serial)
    if index is not None:
        print(
            f"[关闭模拟器] 目标 {serial}（android_id={android_id}）"
            f"→ MuMuManager 反查实例 {index}",
            flush=True,
        )
    else:
        index = _index_from_serial(serial)
        if index is None:
            print(
                f"[关闭模拟器] 目标 {serial}（android_id={android_id}）无法反推 MuMu 实例号，"
                "停止（不随便关一台）",
                flush=True,
            )
            return None
        print(
            f"[关闭模拟器] MuMuManager 未上报 {serial} 的实例号，按端口约定 "
            f"port=16384+32×index 反推实例 {index}",
            flush=True,
        )
    # 能按 android_id 认出在线设备，说明它此刻一定在运行，直接进入关闭流程。
    return manager, index


class ShutdownMumuAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        target = resolve_target()
        if target == ALREADY_CLOSED:
            print("[关闭模拟器] 目标实例本来就没开，按成功处理", flush=True)
            return True
        if target is None:
            print("[关闭模拟器] 未找到可确定的 MuMu 12 实例", flush=True)
            return False

        manager, index = target
        print(f"[关闭模拟器] 正在关闭 MuMu 12 实例 {index}", flush=True)
        code, output, error = ensure_mumu.run(
            [manager, "control", "--vmindex", index, "shutdown"], timeout=30
        )
        if code != 0:
            print(f"[关闭模拟器] 关闭失败：{error or output or f'退出码 {code}'}", flush=True)
            return False
        print(f"[关闭模拟器] 已请求关闭 MuMu 12 实例 {index}", flush=True)
        return True
