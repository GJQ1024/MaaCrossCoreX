"""MFA pretask: discover MuMu 12, start it, and configure ADB (does not launch the game)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_ROOT / "config" / "mumu_runtime.json"
_STARTED_AT = time.monotonic()

# 身份锚点与 scripts/shell_device_binding.py 共用同一份实现（agent/device_identity.py），
# 只认一个 android_id；真机等其它装了游戏的设备直接滤掉，不参与歧义判定。
# 壳自带的嵌入式 Python 用 python312._pth 固定 sys.path，不会自动加上脚本目录，
# 不显式插就 import 不到同目录模块（2026-11-11 上机 pretask 因此退出码 1）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from device_identity import (  # noqa: E402
    CONFIG_FILE as IDENTITY_FILE,
    clear_saved_target,
    filter_by_target,
    pick_entry,
    save_target_android_id,
    target_android_id,
)
import mumu_locator  # noqa: E402  MuMu / adb 定位唯一来源（与 pretask_landscape 共用）


def _configure_console():
    """中文 Windows（cp936）兜底：不可编码字符替换掉，而不是让 print 抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


_configure_console()


def _safe_print(message):
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        # 按当前 stdout 编码做 replace 降级；日志绝不允许打断流程。
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            print(message.encode(encoding, "replace").decode(encoding, "replace"), flush=True)
        except Exception:  # noqa: BLE001
            pass


def elapsed():
    """从脚本启动到现在的秒数，用于定位卡在哪一步。"""
    return time.monotonic() - _STARTED_AT
# 启动任务的 entry 目前是「启动_资源前」。旧包/旧配置里的「进入首页」「StartGameTask」
# 仍然保留：用户升级后可能还留着历史 default.json，删掉会让 pretask 误判"没勾启动游戏"
# 而不启动模拟器，所以这里做兼容并集，而不是只认新名。
START_ENTRIES = {"启动_资源前", "进入首页", "StartGameTask"}
INSTANCE_OPTION = "MuMu实例"
AUTOSTART_OPTION = "模拟器自动启动"
REDETECT_OPTION = "每次重新检测连接"
# 认包不认端口：端口由模拟器分配、会漂移，也会被 is_main 抢走；装没装游戏才是客观事实。
# 官服/B服包名都算已知；运行时优先按设备实际安装情况探测，探测不到才看 MFA_GAME_PACKAGE。
OFFICIAL_GAME_PACKAGE = "com.megagame.crosscore"
BILI_GAME_PACKAGE = "com.megagame.crosscore.bilibili"
KNOWN_GAME_PACKAGES = (OFFICIAL_GAME_PACKAGE, BILI_GAME_PACKAGE)
CONFIGURED_GAME_PACKAGE = os.environ.get("MFA_GAME_PACKAGE", "").strip()
# 当前生效包名（探测后更新；空串 = 未探测到且未配置，只按 android_id 识别）
GAME_PACKAGE = CONFIGURED_GAME_PACKAGE

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_AMBIGUOUS = 3


# MCC 调 pretask 时 stdout 不会进 MFA 日志，所以额外落一份文件日志，
# 否则每次排查这块都是黑盒（只能看到「卡了 N 秒然后 NOT_STARTED」）。
LOG_FILE = PROJECT_ROOT / "logs" / "ensure_mumu.log"


def log(message):
    try:
        _safe_print(f"[MuMu pretask] {message}")
    except Exception:  # noqa: BLE001  日志失败绝不影响 pretask 本身
        pass
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        pass  # 日志失败绝不影响 pretask 本身


def run(args, timeout=30):
    try:
        result = subprocess.run(
            [str(value) for value in args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def load_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {} if default is None else default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def instance_files():
    directory = PROJECT_ROOT / "config" / "instances"
    return sorted(directory.glob("*.json")) if directory.exists() else []


def selected_instance():
    explicit_path = os.environ.get("MFA_INSTANCE_CONFIG_PATH")
    if explicit_path:
        path = Path(explicit_path)
        if path.is_file():
            return path, load_json(path)
        log(f"MFA 指定的实例配置不存在：{path}")

    fallback = None
    for path in instance_files():
        data = load_json(path)
        fallback = fallback or (path, data)
        for task in data.get("TaskItems", []):
            if task.get("entry") in START_ENTRIES and task.get("default_check", False):
                return path, data
    return fallback or (None, {})


def start_game_selected(instance):
    return any(
        task.get("entry") in START_ENTRIES and task.get("default_check", False)
        for task in instance.get("TaskItems", [])
    )


def start_task(instance):
    return next(
        (task for task in instance.get("TaskItems", []) if task.get("entry") in START_ENTRIES),
        {},
    )


def interface_data():
    for path in (PROJECT_ROOT / "interface.json", PROJECT_ROOT / "assets" / "interface.json"):
        if path.is_file():
            return load_json(path)
    return {}


def task_option_case(instance, option_name, default):
    selected = next(
        (option for option in start_task(instance).get("option", []) if option.get("name") == option_name),
        None,
    )
    definition = interface_data().get("option", {}).get(option_name, {})
    cases = definition.get("cases", [])
    if selected is None:
        return str(definition.get("default_case", default))
    index = selected.get("index", 0)
    if isinstance(index, int) and 0 <= index < len(cases):
        return str(cases[index].get("name", default))
    return default


def runtime_settings(instance):
    configured_index = task_option_case(instance, INSTANCE_OPTION, "自动")
    env_index = os.environ.get("MUMU_VM_INDEX")
    requested_index = env_index if env_index is not None else configured_index
    requested_index = int(requested_index) if str(requested_index).isdigit() else None
    # 默认不再由自动化 launch 实例（2026-11-02 决策，见 docs/device.md 已知问题）：
    # 实测短间隔重复 launch 后 MuMuNxDevice.exe 会在 2-4 秒内崩溃。
    # 只有显式设置 AUTO_LAUNCH=1（且实例配置里的「模拟器自动启动」没关）才允许尝试一次。
    env_switch = os.environ.get("AUTO_LAUNCH", "").strip().lower() in ("1", "true", "yes")
    option_start = task_option_case(instance, AUTOSTART_OPTION, "开启") != "关闭"
    return {
        "vm_index": requested_index,
        "auto_start": env_switch and option_start,
        "redetect": task_option_case(instance, REDETECT_OPTION, "关闭") == "开启",
        "minimize_after_launch": bool(instance.get("MinimizeEmulatorAfterLaunch", False)),
    }


def find_mumu():
    """兼容旧调用点（tools/ 与 shutdown_mumu 会 patch 这个函数名）。"""
    return mumu_locator.find_mumu(PROJECT_ROOT, preferred=[load_json(CACHE_FILE).get("root")])


def find_adb(root):
    return mumu_locator.find_adb(root)


def manager_info(manager, index):
    code, output, _ = run([manager, "info", "--vmindex", index], timeout=12)
    if code or not output:
        return {}
    try:
        info = json.loads(output)
        return info if info.get("error_code", 0) == 0 and "index" in info else {}
    except json.JSONDecodeError:
        return {}


def manager_info_all(manager):
    """一次读取全部实例（info -v all），返回 {index: info}。"""
    code, output, _ = run([manager, "info", "-v", "all"], timeout=20)
    if code or not output:
        return {}
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        if str(key).isdigit() and isinstance(value, dict):
            result[int(key)] = value
    return result


def detect_single_instance(manager):
    """未配置实例号时：实际只有 1 台实例才采纳它，否则返回 None（不猜）。

    实例配置里的「MuMu实例」是显式覆盖，调用方应优先使用；本函数只在
    配置/缓存都没有实例号时兜底，避免单实例机器因默认 vmindex 写错而中止。
    """
    instances = manager_info_all(manager)
    if len(instances) == 1:
        index = next(iter(instances))
        log(
            f"[身份] 未配置实例号：MuMu 只有 1 台实例，使用实例 {index}"
            "（实例配置里的值优先于该探测）"
        )
        return index
    return None


def residual_headless_process():
    """是否还有残留的 MuMuVMMHeadless（VM 进程没退干净）。"""
    code, output, _ = run(
        ["tasklist", "/FI", "IMAGENAME eq MuMuVMMHeadless.exe", "/FO", "CSV", "/NH"],
        timeout=10,
    )
    return code == 0 and "mumuvmmheadless.exe" in output.lower()


def port_in_use(port):
    """目标 adb 端口是否已被占用（能连上就说明有别的进程在监听）。"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1.0):
            return True
    except OSError:
        return False


def launch_blockers(index, info):
    """AUTO_LAUNCH 的前置检查；返回原因列表，空列表才允许 launch。

    教训（2026-11-02）：上一轮崩溃后的残留 VM / 短间隔重复 launch 会触发
    MuMuNxDevice.exe 0xc0000409（21/22 次进程只活了 2-4 秒）。宁可放弃本次任务，
    也不要在异常状态下再拉一次实例。
    """
    reasons = []
    if info.get("is_process_started") or info.get("is_android_started"):
        reasons.append("实例进程仍在运行")
    if residual_headless_process():
        reasons.append("存在残留 MuMuVMMHeadless 进程（残留 VM 未清理）")
    port = info.get("adb_port")
    if port is None:
        reasons.append("未能读取实例 adb 端口，无法确认端口占用")
    elif port_in_use(port):
        reasons.append(f"adb 端口 {port} 已被占用")
    return reasons


def ensure_android(manager, index, info, allow_start=True):
    if not info.get("is_process_started") and not info.get("is_android_started"):
        label = info.get("name") or f"实例 {index}"
        # 诊断：这条分支就是「卡 60 秒」的源头，把判断依据的原始值和已耗时打出来
        log(
            "诊断：实例 %s 判定为未启动 —— is_process_started=%r is_android_started=%r，"
            "此时已耗时 %.1fs"
            % (index, info.get("is_process_started"), info.get("is_android_started"), elapsed())
        )
        if not allow_start:
            log(
                f"MuMu「{label}」(实例 {index}) 未在运行。请先手动打开 MuMu 中的"
                f"「{label}」实例并等 Android 启动完成，再重新开始任务"
                f"（自动化默认不替你启动模拟器；如需自动启动请显式设置 AUTO_LAUNCH=1）"
            )
            return {}
        blockers = launch_blockers(index, info)
        if blockers:
            log(
                "未执行 launch：前置检查未通过 —— " + "；".join(blockers)
                + "（为避免 MuMuNxDevice 崩溃，本次放弃启动，不做重试）"
            )
            return {}
        log(f"正在启动 MuMu 12 实例 {index}（AUTO_LAUNCH=1，仅尝试一次，失败即停）")
        launch_started = time.monotonic()
        code, output, error = run(
            [manager, "control", "--vmindex", index, "launch"], timeout=60
        )
        log(
            "诊断：launch 返回 code=%r，耗时 %.1fs（output=%r error=%r）"
            % (code, time.monotonic() - launch_started, (output or "")[:200], (error or "")[:200])
        )
        if code:
            log(f"启动 MuMu 失败：{error or output}")
            return {}
    else:
        log(f"MuMu 12 实例 {index} 已在运行，等待 Android 就绪")
    for _ in range(150):
        current = manager_info(manager, index)
        if current.get("is_android_started"):
            return current
        # MuMu 12 的 is_android_started 有假阴性（手动打开的实例实测报 false，
        # 但 player_state=start_finished、ADB shell 可用）；此处不再干等 150 秒，
        # 交给后面的 ensure_adb 用 ADB 实测确认。
        if current.get("player_state") == "start_finished":
            log("is_android_started=false，但 player_state=start_finished，交由 ADB 实测确认")
            return current
        time.sleep(1)
    log("MuMu Android 启动超时")
    return {}


def minimize_mumu(manager, index):
    """最小化指定 MuMu 实例的主窗口；失败只记录，不中断任务。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        for _ in range(20):
            info = manager_info(manager, index)
            handle_text = str(info.get("main_wnd") or "").strip()
            try:
                handle = int(handle_text, 16)
            except ValueError:
                handle = 0
            if handle and user32.IsWindow(handle):
                if user32.ShowWindowAsync(handle, 6):  # SW_MINIMIZE
                    log(f"已最小化 MuMu 实例 {index}")
                    return True
            time.sleep(0.25)
    except Exception as exc:
        log(f"最小化 MuMu 实例 {index} 失败：{exc}")
        return False
    log(f"未找到 MuMu 实例 {index} 的可最小化窗口")
    return False


def adb_devices(adb):
    _, output, _ = run([adb, "devices"], timeout=20)
    devices = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] != "List":
            devices[fields[0]] = fields[1]
    return devices


def adb_state(adb, serial):
    code, output, _ = run([adb, "-s", serial, "get-state"], timeout=12)
    return output.strip() if code == 0 else ""


def adb_shell_ok(adb, serial):
    code, output, error = run(
        [adb, "-s", serial, "shell", "echo", "maa-ok"], timeout=8
    )
    return code == 0 and "maa-ok" in f"{output}\n{error}"


def instance_serial(info):
    """MuMuManager info → adb 序列号；读不到端口时返回空串。"""
    port = info.get("adb_port")
    if not port:
        return ""
    host = str(info.get("adb_host_ip") or "127.0.0.1")
    return f"{host}:{port}"


def index_for_serial(manager, serial):
    """按 ADB serial 反查 MuMu 实例编号。

    配置里的 extras.mumu.index 不可信：历史上它与实际 AdbSerial 不一致过
    （serial 是 <实例> 的 <port>，index 却还是 <其它实例> 的 0），壳存盘时会把
    错误编号继续传下去。端口只是运行时地址，serial→index 的唯一权威是
    MuMuManager info；这里只做反查，不推导、不猜。
    """
    if not serial:
        return None
    for index, info in manager_info_all(manager).items():
        if instance_serial(info) == serial:
            return index
    return None


def package_label(package):
    if package == OFFICIAL_GAME_PACKAGE:
        return f"{package}（官服）"
    if package == BILI_GAME_PACKAGE:
        return f"{package}（B服）"
    return package or "官服/B服（未配置）"


def installed_game_packages(adb, serial):
    """读取设备已安装的官服/B服包；adb 失败返回 []。只读查询。"""
    code, output, _ = run(
        [adb, "-s", serial, "shell", "pm", "list", "packages"],
        timeout=20,
    )
    if code != 0:
        return []
    installed = set()
    for line in output.splitlines():
        name = line.strip()
        if name.startswith("package:"):
            name = name[len("package:"):]
        if name in KNOWN_GAME_PACKAGES:
            installed.add(name)
    return [package for package in KNOWN_GAME_PACKAGES if package in installed]


def detect_game_package(adb, serial):
    """优先用设备实际安装的包；探测不到才退回 MFA_GAME_PACKAGE，并说明用了哪个。"""
    global GAME_PACKAGE
    installed = installed_game_packages(adb, serial)
    if len(installed) == 1:
        GAME_PACKAGE = installed[0]
        log(f"[身份] 探测到已安装包：{package_label(GAME_PACKAGE)}")
    elif len(installed) > 1:
        preferred = (
            CONFIGURED_GAME_PACKAGE
            if CONFIGURED_GAME_PACKAGE in installed
            else OFFICIAL_GAME_PACKAGE
        )
        GAME_PACKAGE = preferred
        log(
            f"[身份] 同时装有官服与B服 {installed}，按 {package_label(GAME_PACKAGE)} 处理"
            "（可用 MFA_GAME_PACKAGE 覆盖）"
        )
    elif CONFIGURED_GAME_PACKAGE:
        GAME_PACKAGE = CONFIGURED_GAME_PACKAGE
        log(f"[身份] 未探测到官服/B服包，沿用配置 MFA_GAME_PACKAGE={GAME_PACKAGE}")
    else:
        GAME_PACKAGE = ""
        log("[身份] 未探测到官服/B服包，且未配置 MFA_GAME_PACKAGE；只按 android_id 识别")
    return GAME_PACKAGE


def adb_package_hit(adb, serial):
    """该实例是否装了官服/B服游戏包（未显式配置时任一即可）。只读查询。"""
    if not serial:
        return False
    if adb_state(adb, serial) != "device":
        run([adb, "connect", serial], timeout=15)
        if adb_state(adb, serial) != "device":
            return False
    # adb shell 偶发瞬时失败，两次都判不出才算没有。
    for _ in range(2):
        installed = installed_game_packages(adb, serial)
        if installed:
            return (
                CONFIGURED_GAME_PACKAGE in installed
                if CONFIGURED_GAME_PACKAGE
                else True
            )
    return False


def adb_android_id(adb, serial):
    """读取设备的 android_id；取不到返回空串。只读查询。

    部分机型/未授权状态下 `settings get secure android_id` 会返回字面量
    unknown/null，这些同样视为「取不到」，不能拿去和目标 id 比较。
    """
    code, output, _ = run(
        [adb, "-s", serial, "shell", "settings", "get", "secure", "android_id"],
        timeout=12,
    )
    if code != 0:
        return ""
    value = output.strip()
    return "" if value.lower() in ("", "unknown", "null", "none") else value


class TargetResolutionError(Exception):
    """无法唯一确定目标设备；调用方必须硬失败，不允许自动挑。"""


class NoTarget(TargetResolutionError):
    """0 台目标设备。"""


class AmbiguousTarget(TargetResolutionError):
    """≥2 台目标设备，身份歧义。"""


class IdentityUnavailable(TargetResolutionError):
    """读不到 android_id（空/unknown/命令失败），不能当成「读到但不匹配」。"""


def rebind_requested():
    """--rebind 或 MFA_REBIND=1：清掉自动绑定的 android_id，按当前在线设备重新绑定。"""
    env_switch = os.environ.get("MFA_REBIND", "").strip().lower() in ("1", "true", "yes")
    return env_switch or "--rebind" in sys.argv[1:]


def resolve_adb_target(adb, preferred_serial=""):
    """按目标 android_id + 游戏包唯一识别目标设备。

    身份锚点与 binder 完全一致（device_identity.target_android_id()：环境变量 >
    config/device_identity.json > 空）。未配置目标时，若「唯一一台装了游戏的在线
    设备」能读到 android_id，则自动绑定并持久化，后续启动直接复用。
    先按目标 android_id 过滤候选，真机等非目标设备只在日志留一行摘要（带实际读到的
    值）；过滤后再去重与歧义判断。0 台、≥2 个不同 id 时抛 TargetResolutionError
    的子类；调用方必须硬失败，不允许按 adb 顺序或实例编号自动挑一台。
    """
    devices = adb_devices(adb)
    online = [serial for serial, state in devices.items() if state == "device"]
    log(
        f"[身份] 包名={package_label(CONFIGURED_GAME_PACKAGE)}"
        f"（未配置时按设备实际安装的官服/B服包探测）；在线设备 {len(online)} 台"
    )
    with_game = []
    unreadable = []
    for serial in online:
        if not adb_package_hit(adb, serial):
            log(f"[身份] 候选 {serial} → 无官服/B服包，跳过")
            continue
        aid = adb_android_id(adb, serial) or ""
        if not aid:
            unreadable.append(serial)
            log(
                f"[身份] 候选 {serial} 装了官服/B服包，"
                "但取不到 android_id（空/unknown/命令失败）"
            )
        with_game.append((serial, aid))

    target = target_android_id()
    if not target:
        readable = [(serial, aid) for serial, aid in with_game if aid]
        if len(with_game) == 1 and len(readable) == 1:
            serial, aid = readable[0]
            if save_target_android_id(aid):
                target = aid
                log(
                    f"[身份] 未配置目标 android_id，已自动绑定 {serial} → android_id={aid}"
                    f"（写入 {IDENTITY_FILE}；需要重新绑定请加 --rebind）"
                )
            else:
                log("[身份] 未配置目标 android_id，自动绑定失败，本次不做指纹过滤")
        elif len(with_game) > 1:
            log("[身份] 未配置目标 android_id：不做指纹过滤，多台设备将按歧义规则停止")
        elif not with_game:
            log("[身份] 未配置目标 android_id：没有装了官服/B服包的在线设备")
        else:
            log("[身份] 未配置目标 android_id：候选设备取不到 android_id，本次不做指纹绑定")

    candidates, dropped = filter_by_target(with_game, target)
    detail = "、".join(f"{serial}={aid or '取不到'}" for serial, aid in with_game)
    if dropped:
        log(f"[身份] 目标 android_id={target}：过滤掉 {dropped} 台非目标设备（{detail}），不参与判定")
    for serial, aid in candidates:
        log(f"[身份] 目标设备 {serial} → android_id={aid or '（取不到）'}")
    if not candidates:
        if unreadable:
            # 读不到指纹 ≠ 读到了但不是目标：分别报，便于用户判断是换机还是 adb 异常
            raise IdentityUnavailable(
                f"目标 android_id={target}；有设备取不到 android_id（{detail}），"
                "不能判定为不匹配（可能是未授权、adb shell 失败或系统返回 unknown）"
            )
        raise NoTarget(f"目标 android_id={target} 未匹配到设备（实际读到：{detail or '无候选设备'}）")

    groups: dict[str, list[str]] = {}
    for serial, aid in candidates:
        groups.setdefault(aid, []).append(serial)
    if len(groups) > 1:
        log(f"[身份] 匹配到 {len(groups)} 台不同 android_id 的设备，身份歧义，停止（不自动挑）：{candidates}")
        raise AmbiguousTarget(f"{len(groups)} 台")
    aid, serials = next(iter(groups.items()))
    serial = pick_entry(serials)
    if len(serials) > 1:
        log(f"[身份] android_id={aid} 有 {len(serials)} 个入口 {serials}，视为同一台，选 {serial}")
    if preferred_serial and serial != preferred_serial:
        log(
            f"[身份] 配置 serial={preferred_serial} 与 android_id={aid} "
            f"对应的 {serial} 不符，按 android_id 纠正"
        )
    return serial, aid


def describe_instance(index, info, serial=""):
    """给用户看：实例名 + index + 端口。端口只是运行时地址，不作为判定依据。"""
    name = str(info.get("name") or "MuMu 实例")
    address = serial or instance_serial(info) or "端口未知"
    return f"{name} (index {index}) {address}"


def connection_usable(adb, serial, instance):
    """adb devices 的 device 只代表端口还在握手，不代表 Android 能跑 shell。"""
    if not serial:
        return False
    root, index = device_root_index(instance, adb)
    manager = mumu_locator.find_manager(root) if root else None
    if manager and manager.is_file():
        actual = index_for_serial(manager, serial)
        if actual is not None and actual != index:
            log(f"配置中的实例号 {index} 与 ADB {serial} 不符，按 serial 校正为 {actual}")
            index = actual
        info = manager_info(manager, index) if index is not None else {}
        if info and not info.get("is_android_started"):
            name = info.get("name") or f"实例 {index}"
            state = info.get("player_state") or "unknown"
            # MuMu 12 的 is_android_started 偶发假阴性（player_state=start_finished
            # 但 ADB shell 实际可用）。不直接判死，交由下面的 ADB 实测决定。
            log(
                f"MuMu「{name}」(实例 {index}) is_android_started=false"
                f"（player_state={state}），交由 ADB 实测判断是否可用"
            )
    if adb_state(adb, serial) != "device":
        return False
    if not adb_shell_ok(adb, serial):
        log(f"ADB {serial} 显示 device，但 shell 无响应")
        return False
    return True


def recover_existing_adb(adb, serial, instance, allow_hard_restart=True):
    if connection_usable(adb, serial, instance):
        return True
    log(f"已保存的 ADB {serial} 不可用，尝试重新连接")
    if ":" in serial:
        run([adb, "disconnect", serial], timeout=10)
        run([adb, "connect", serial], timeout=15)
    if connection_usable(adb, serial, instance):
        return True

    devices = adb_devices(adb)
    other_online = [
        device for device, state in devices.items() if device != serial and state == "device"
    ]
    if not allow_hard_restart or other_online:
        if other_online:
            log("检测到其他在线 ADB 设备，跳过全局 ADB Server 重启")
        return False

    log("普通重连失败，重启 ADB Server 后再试一次")
    run([adb, "kill-server"], timeout=15)
    run([adb, "start-server"], timeout=20)
    if ":" in serial:
        run([adb, "connect", serial], timeout=15)
    return connection_usable(adb, serial, instance)


def resolve_adb_path(value):
    """把配置里的 AdbPath 解析成可执行文件；解析不到返回 ""（绝不回落裸 adb）。

    指纹形状要求 AdbPath="adb"（依赖 PATH），不能再用 Path("adb").is_file()
    误判成「没有保存连接」。实际定位统一交给 mumu_locator（与 pretask_landscape 同序）。
    """
    try:
        return mumu_locator.resolve_adb(PROJECT_ROOT, configured=value)
    except mumu_locator.MuMuNotFound:
        return ""


def saved_connection(instance):
    device = instance.get("AdbDevice", {}) or {}
    serial = str(device.get("AdbSerial", "")).strip()
    adb = resolve_adb_path(device.get("AdbPath", ""))
    if adb and serial:
        return Path(adb), serial
    return None


def device_root_index(instance, adb):
    """从已有 AdbDevice.Config / 路径推断 MuMu root 与实例号，供写回 MFA。"""
    device = instance.get("AdbDevice", {}) or {}
    index = None
    root = None
    try:
        extras = json.loads(str(device.get("Config") or "{}")).get("extras", {}).get("mumu", {})
        if extras.get("path"):
            root = Path(str(extras["path"]))
        if extras.get("index") is not None and str(extras["index"]).isdigit():
            index = int(extras["index"])
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    if root is None or not root.exists():
        # adb 可能在 nx_main / shell / nx_device\*\shell 等任意布局，统一归一。
        candidate = mumu_locator.root_from(adb)
        if candidate is not None and mumu_locator.find_manager(candidate):
            root = candidate
    if root is None:
        found, _, _ = find_mumu()
        root = found
    return root, index


def ensure_adb(adb, info, index):
    run([adb, "start-server"], timeout=20)
    host = str(info.get("adb_host_ip") or "127.0.0.1")
    port = info.get("adb_port")
    tcp = f"{host}:{port}" if port else ""
    emulator = f"emulator-{5554 + index * 2}"
    for _ in range(50):
        devices = adb_devices(adb)
        for serial in (tcp, emulator):
            if serial and devices.get(serial) == "device" and adb_shell_ok(adb, serial):
                return serial
        if tcp:
            if devices.get(tcp) == "offline":
                run([adb, "disconnect", tcp], timeout=10)
            run([adb, "connect", tcp], timeout=15)
        time.sleep(0.5)
    return ""


SCREENCAP_ALL_METHODS = 18446744073709551559  # MFA 基线：所有截图方式都可用


def _normalize_screencap(value):
    """把只启用 EmulatorExtras(64) 的收窄配置归一为全方法位掩码。

    证据（2026-11-03 23:41 maafw.log）：ScreencapMethods=64 时 MaaFramework
    只试 MuMuPlayerExtras，首次截图失败后连接中断，GUI 却仍报"任务已全部完成"。
    """
    try:
        current = int(value) if value not in (None, "") else SCREENCAP_ALL_METHODS
    except (TypeError, ValueError):
        current = SCREENCAP_ALL_METHODS
    if current == 64:
        log("ScreencapMethods=64（仅 EmulatorExtras）已归一为全方法，避免截图失败")
        return SCREENCAP_ALL_METHODS
    return current or SCREENCAP_ALL_METHODS


def update_instance(path, instance, adb, serial, root, index):
    if not path:
        log(f"无法写回 MFA：实例配置路径为空（serial={serial}）")
        return
    old = instance.get("AdbDevice", {}) or {}
    adb_text = str(adb).replace("\\", "/")
    # 与 binder 使用同一指纹形状：Name=serial-adb完整路径、AdbPath=adb、Config={}。
    # 这样 MatchesFingerprint 的 Name+Address+Port 可稳定 3/3，不依赖实例号。
    device = {
        "Name": f"{serial}-{adb_text}",
        "AdbPath": "adb",
        "AdbSerial": serial,
        # 2026-11-03 事故：GUI 把截图方式收窄成 64（仅 EmulatorExtras）后，连接期
        # 只剩 Extras 可用，首次截图失败直接落到"任务已全部完成"（9 秒假完成）。
        # 这里把"仅 Extras"归一为全方法位掩码，让连接自动回退到其它截图方式。
        "ScreencapMethods": _normalize_screencap(old.get("ScreencapMethods")),
        "InputMethods": int(old.get("InputMethods") or 4),
        "Config": "{}",
        "AgentPath": old.get("AgentPath") or "./MaaAgentBinary",
    }
    # 不保留过期 InfoHandle，否则 MFA 可能仍按 device=<none> 连接
    instance["AdbDevice"] = device
    instance["RememberAdb"] = True
    instance["UseFingerprintMatching"] = True
    manager = mumu_locator.find_manager(root) if root else None
    if manager and manager.is_file():
        # 与 MFA「启动设置 > 游戏路径」使用同一套启动入口。这样连接失败时
        # MFA 可以直接复用本次自动检测到的 MuMu，而不依赖用户手工填写路径。
        instance["SoftwarePath"] = str(manager.resolve())
        if index is not None:
            instance["EmulatorConfig"] = f"control --vmindex {index} launch"
    elif root is None:
        log("未定位 MuMu 安装路径，保留 SoftwarePath/EmulatorConfig 原值")
    save_json(path, instance)
    log(
        f"已写入 MFA 连接：{serial}（android_id 身份；"
        f"指纹形状 Name={device['Name']}, AdbPath=adb, Config={{}}）"
    )
    if manager and manager.is_file():
        log(f"已同步 MFA 游戏路径：{manager.resolve()}")


def main():
    instance_path, instance = selected_instance()
    if os.name != "nt":
        log("MuMu 12 自动启动目前仅支持 Windows")
        return EXIT_FAIL

    if rebind_requested():
        cleared = clear_saved_target()
        log(
            f"[身份] --rebind：已清除自动绑定 {IDENTITY_FILE}"
            if cleared
            else f"[身份] --rebind：清除自动绑定失败 {IDENTITY_FILE}，将按当前在线设备重新识别"
        )
        if os.environ.get("MFA_TARGET_ANDROID_ID", "").strip():
            log("[身份] --rebind：环境变量 MFA_TARGET_ANDROID_ID 仍优先，自动绑定不会生效")

    settings = runtime_settings(instance)
    saved = saved_connection(instance)
    saved_serial = saved[1] if saved else ""
    adb = str(saved[0]) if saved else resolve_adb_path("")
    if not adb:
        _, _, found_adb = find_mumu()
        adb = str(found_adb) if found_adb else ""
    if not adb:
        # 只是宿主侧定位失败，不代表设备异常：不中止整条任务链（2026-11-18）。
        log("警告：未找到可用的 adb.exe，已跳过本次 MuMu/ADB 自动配置；任务链继续")
        log("     壳若已连接设备，后续任务仍可正常执行；需要自动配置时按下面指引填一次 ADB 路径")
        for line in mumu_locator.describe_search(PROJECT_ROOT):
            log("  " + line)
        return EXIT_OK
    run([adb, "start-server"], timeout=20)

    if saved and not settings["redetect"]:
        log(f"[1/3] 检查已保存的 ADB 连接：{saved_serial}")
        if recover_existing_adb(adb, saved_serial, instance):
            try:
                serial, aid = resolve_adb_target(adb, saved_serial)
            except TargetResolutionError as exc:
                log(f"[身份] 已保存连接无法按 android_id 唯一识别（{exc}），停止（不自动挑）")
                return EXIT_AMBIGUOUS
            root, index = device_root_index(instance, adb)
            manager = mumu_locator.find_manager(root) if root else None
            if manager and manager.is_file():
                actual = index_for_serial(manager, serial)
                if actual is not None:
                    index = actual
                info = manager_info(manager, index) if index is not None else {}
                log(f"[连接] 已连接 {describe_instance(index, info, serial) if index is not None else serial}")
            update_instance(instance_path, instance, adb, serial, root, index)
            log("[2/3] 已保存的 ADB 连接可用，跳过模拟器扫描")
            log("[3/3] MuMu 与 ADB 已就绪（不开游戏）")
            return EXIT_OK
        log("已保存连接不可用，改为按 android_id 识别")

    # 不依赖 MuMuManager 的实例号：直接在 adb 在线设备中按包名 + android_id 唯一识别。
    log("[1/3] 按 android_id 识别目标设备")
    try:
        serial, aid = resolve_adb_target(adb, saved_serial)
    except AmbiguousTarget:
        return EXIT_AMBIGUOUS
    except TargetResolutionError:
        pass
    else:
        root, index = device_root_index(instance, adb)
        manager = mumu_locator.find_manager(root) if root else None
        if manager and manager.is_file():
            actual = index_for_serial(manager, serial)
            if actual is not None:
                index = actual
            info = manager_info(manager, index) if index is not None else {}
            log(f"[连接] 已识别 {describe_instance(index, info, serial) if index is not None else serial}")
        detect_game_package(adb, serial)
        update_instance(instance_path, instance, adb, serial, root, index)
        log("[2/3] 已按 android_id 识别唯一目标设备")
        log("[3/3] MuMu 与 ADB 已就绪（不开游戏）")
        return EXIT_OK

    # 0 台：只有显式允许自动启动且存在明确实例号时，才启动该明确实例一次。
    # 禁止 range(10) 盲扫，禁止按实例顺序挑第一台。
    root, manager, _ = find_mumu()
    if not manager:
        log("[身份] 0 台目标设备，且未找到 MuMu 12；停止（不自动挑）")
        return EXIT_AMBIGUOUS
    _, saved_index = device_root_index(instance, adb)
    cached_index = load_json(CACHE_FILE).get("vm_index")
    known_index = settings["vm_index"]
    if known_index is None:
        known_index = saved_index if saved_index is not None else cached_index
    if not settings["auto_start"]:
        log(
            "[身份] 0 台目标设备；自动化默认不启动模拟器，停止（不自动挑）。"
            "请手动打开目标实例后再开始任务，或显式设置 AUTO_LAUNCH=1"
        )
        return EXIT_AMBIGUOUS
    if known_index is None:
        # 配置/缓存都没有时，才用 MuMuManager 的实际实例列表：只有一台就采纳它。
        known_index = detect_single_instance(manager)
    if known_index is None:
        log("[身份] 0 台目标设备，且没有明确的已知实例号；拒绝盲扫/自动挑，停止")
        return EXIT_AMBIGUOUS
    info = manager_info(manager, int(known_index))
    if not info:
        log(f"[身份] 明确实例 {known_index} 读取失败；停止（不自动挑）")
        return EXIT_AMBIGUOUS
    launched_by_pretask = not info.get("is_process_started") and not info.get("is_android_started")
    log(f"[2/3] 启动明确实例 {describe_instance(int(known_index), info)}")
    info = ensure_android(manager, int(known_index), info, allow_start=True)
    if not info:
        return EXIT_AMBIGUOUS
    # 只等待这个「明确实例」的 adb 端口上线，不做任何按顺序挑选。
    target_serial = ensure_adb(adb, info, int(known_index))
    if not target_serial:
        log("[身份] 明确实例启动后 ADB 未就绪，停止（不自动挑）")
        return EXIT_AMBIGUOUS
    try:
        serial, aid = resolve_adb_target(adb, saved_serial)
    except TargetResolutionError:
        log("[身份] 启动明确实例后仍无法按 android_id 唯一识别，停止（不自动挑）")
        return EXIT_AMBIGUOUS
    detect_game_package(adb, serial)
    save_json(CACHE_FILE, {"root": str(root), "vm_index": int(known_index), "adb_serial": serial})
    update_instance(instance_path, instance, adb, serial, root, int(known_index))
    if launched_by_pretask and settings["minimize_after_launch"]:
        minimize_mumu(manager, int(known_index))
    log("[3/3] MuMu 与 ADB 已就绪（不开游戏）")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
