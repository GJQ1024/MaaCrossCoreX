# -*- coding: utf-8 -*-
"""MuMu 12 安装目录与 adb 的统一定位（ensure_mumu / pretask_landscape 共用）。

2026-11-18 起以「显式锚点」为主，不再依赖安装目录名（定制版可能叫
``D:\\YXCrossCore-12.0`` 之类）。查找顺序：

  1. 显式覆盖：环境变量 ``MUMU_MANAGER`` / ``MUMU_HOME`` / ``MUMU_ROOT`` /
     ``MUMU_ADB`` / ``ADB_PATH``；
  2. 实例配置里的路径锚点：``SoftwarePath``、``AdbDevice.AdbPath``、
     ``AdbDevice.Name``（形如 ``serial-D:/.../adb.exe``）、``AdbDevice.Config``
     的 ``extras.mumu.path``；
  3. 运行中的进程 exe 路径（MuMuPlayer.exe / MuMuNxDevice.exe 等），从 exe
     向上找带 MuMu 布局的目录——与 MaaToolkit ``AdbDeviceFinder`` 同思路，
     对任意安装目录（含非默认盘符）都成立；
  4. 注册表卸载项 / ``App Paths`` / ``Software\\Netease``；
  5. 布局扫描：各盘符顶层目录、Program Files 下一层，只认 ``nx_main`` /
     ``shell`` / ``nx_device`` 等布局标记，不认目录名；
  6. 上述根的已知布局 → 有界深度搜索（``adb.exe`` / ``adb_server.exe``）；
  7. PATH（``shutil.which("adb")``，仍返回绝对路径）。

关键点（2026-11-18 修复）：
  * adb 定位与 ``MuMuManager.exe`` **解耦**——管理器不在 ``nx_main`` 也不影响
    用同一安装根下的 adb；
  * ``SoftwarePath=...\\shell\\MuMuPlayer.exe`` 会归一到安装根，而不是 ``shell``；
  * adb 优先用模拟器自带的（``nx_device\\*\\shell`` → ``shell`` → ``nx_main``），
    避免把「找不到」换成「版本不匹配」。

**绝不返回裸 ``adb``**：全部落空时抛 :class:`MuMuNotFound`，消息内列出已检查的位置。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import string


MANAGER_REL = Path("nx_main") / "MuMuManager.exe"
MANAGER_LAYOUTS = (
    Path("nx_main") / "MuMuManager.exe",
    Path("shell") / "MuMuManager.exe",
    Path("MuMuManager.exe"),
)
ADB_LAYOUTS = (
    Path("shell") / "adb.exe",
    Path("nx_main") / "adb.exe",
    Path("adb.exe"),
    Path("emulator") / "adb.exe",
    Path("adb") / "adb.exe",
    Path("vmonitor") / "bin" / "adb_server.exe",
    Path("MuMu") / "emulator" / "nemu" / "vmonitor" / "bin" / "adb_server.exe",
    Path("platform-tools") / "adb.exe",
    Path("tools") / "adb.exe",
)
ROOT_MARKERS = (
    MANAGER_REL,
    Path("nx_main") / "MuMuNxDevice.exe",
    Path("nx_main") / "adb.exe",
    Path("shell") / "MuMuPlayer.exe",
    Path("shell") / "MuMuManager.exe",
    Path("shell") / "adb.exe",
)
ADB_NAMES = ("adb.exe", "adb_server.exe")
INSTALL_NAMES = ("MuMu", "MuMu 12", "MuMuPlayer-12.0", "MuMu Player 12")
RUNTIME_CACHE = Path("config") / "mumu_runtime.json"

# 目录扫描/深度搜索的边界，避免在真实安装根（含系统镜像）里失控。
MAX_CHILDREN = 400
MAX_WALK_FILES = 20000
WALK_DEPTH = 4
WALK_SKIP_DIRS = {
    "vms", "cache", "logs", "log", "temp", "tmp", "crashpad",
    "node_modules", ".git", "dumps", "backup", "backups",
}

_DRIVE_PREFIX = re.compile(r"[A-Za-z]:[\\/]")
_CACHE: dict = {}


class MuMuNotFound(RuntimeError):
    """找不到 MuMu 安装目录或 adb；消息里包含已检查的位置和可操作的修法。"""


def reset_cache() -> None:
    """清空进程内的探测缓存（测试或安装目录变更后使用）。"""
    _CACHE.clear()


def _cached(key):
    return _CACHE.get(key)


def _store(key, value):
    _CACHE[key] = value
    return value


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _looks_like_root(directory: Path) -> bool:
    """目录里存在任意 MuMu 布局标记即视为安装根（不看目录名）。"""
    if not _is_dir(directory):
        return False
    if any(_is_file(directory / marker) for marker in ROOT_MARKERS):
        return True
    devices = directory / "nx_device"
    if _is_dir(devices):
        try:
            for device in devices.iterdir():
                if _is_dir(device) and _is_file(device / "shell" / "adb.exe"):
                    return True
        except OSError:
            pass
    return False


def _root_from(value) -> Path | None:
    """把目录 / MuMuManager.exe / MuMuPlayer.exe / adb.exe / 其它 exe 归一成安装根。"""
    if value in (None, ""):
        return None
    try:
        path = Path(str(value).strip().strip('"')).expanduser()
    except OSError:
        return None

    if _is_file(path):
        start = path.parent
        # 先按真实布局向上找根：shell\MuMuPlayer.exe → 安装根。
        # 取最外层命中项：nx_device\12.0\shell\MuMuNxDevice.exe 归到安装根，
        # 而不是归到 nx_device\12.0（否则 MuMuManager 的兄弟目录就找不到了）。
        found: Path | None = None
        try:
            for candidate in (start, *list(start.parents)[:4]):
                if _looks_like_root(candidate):
                    found = candidate
        except OSError:
            pass
        if found is not None:
            return found
        name = path.name.lower()
        if name == "mumumanager.exe":
            return start.parent
        if name == "adb.exe":
            for parent in path.parents:
                if parent.name.lower() == "nx_main":
                    return parent.parent
            return None
        if start.name.lower() in ("shell", "nx_main"):
            return start.parent
        return start

    if _is_dir(path):
        if _looks_like_root(path):
            # 同上：子层（nx_device\12.0）向上归一到最外层安装根。
            outermost = path
            try:
                for parent in list(path.parents)[:2]:
                    if parent.parent == parent:
                        break
                    if _looks_like_root(parent):
                        outermost = parent
            except OSError:
                pass
            return outermost
        if path.name.lower() in ("shell", "nx_main"):
            return path.parent
        return path
    return None


def root_from(value) -> Path | None:
    """公开的路径归一入口（ensure_mumu 等模块复用，避免各自猜层级）。"""
    return _root_from(value)


def _paths_in_text(text: str) -> list[Path]:
    """从 ``serial-D:/path/adb.exe`` 这类文本里提取 adb 绝对路径。"""
    if not text:
        return []
    values: list[Path] = []
    for match in re.finditer(r"(?i)adb\.exe", text):
        for prefix in _DRIVE_PREFIX.finditer(text[: match.end()]):
            if prefix.start() and text[prefix.start() - 1].isalnum():
                continue
            segment = text[prefix.start() : match.end()]
            if re.search(r"[|<>\"'\r\n]", segment):
                continue
            values.append(Path(segment))
            break
    return values


def _as_paths(value) -> list[Path]:
    """把配置值转成可探测的路径；裸 ``adb`` / ``adb.exe`` 不当作路径。"""
    if not isinstance(value, str):
        return []
    text = value.strip().strip('"')
    if not text or text.lower() in ("adb", "adb.exe"):
        return []
    embedded = _paths_in_text(text)
    if embedded:
        return embedded
    if _DRIVE_PREFIX.match(text) or text.startswith("\\\\"):
        return [Path(text.split(",")[0].strip())]
    return []


def instance_config_paths(project_root: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        if not path.is_file():
            return
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            paths.append(path)

    for key in ("MFA_INSTANCE_CONFIG_PATH", "MFA_INSTANCE_CONFIG"):
        value = os.environ.get(key, "").strip()
        if value:
            add(Path(value))
    directory = project_root / "config" / "instances"
    if directory.is_dir():
        add(directory / "default.json")
        for path in sorted(directory.glob("*.json")):
            add(path)
    return paths


def configured_paths(project_root: Path) -> list[Path]:
    """实例配置里所有能当锚点的路径（SoftwarePath / AdbDevice.Name / AdbPath ...）。"""
    values: list[Path] = []
    for path in instance_config_paths(project_root):
        data = _read_json(path)
        values.extend(_as_paths(data.get("SoftwarePath")))
        device = data.get("AdbDevice")
        if not isinstance(device, dict):
            continue
        values.extend(_as_paths(device.get("AdbPath")))
        values.extend(_paths_in_text(str(device.get("Name") or "")))
        config = device.get("Config")
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        if isinstance(config, dict):
            extras = config.get("extras")
            if isinstance(extras, dict):
                mumu = extras.get("mumu")
                if isinstance(mumu, dict):
                    for key in ("path", "adb", "adb_path", "install_path"):
                        values.extend(_as_paths(mumu.get(key)))
    return values


def software_paths(project_root: Path) -> list[Path]:
    """实例配置里显式写的 ``SoftwarePath``（兼容旧调用）。"""
    values: list[Path] = []
    for path in instance_config_paths(project_root):
        values.extend(_as_paths(_read_json(path).get("SoftwarePath")))
    return values


def _reg_value(key, name: str) -> str:
    try:
        import winreg

        return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ValueError, TypeError):
        return ""


def _registry_roots_uncached() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    roots: list[Path] = []
    uninstall_keys = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, key_name in uninstall_keys:
        try:
            with winreg.OpenKey(hive, key_name) as root:
                count = winreg.QueryInfoKey(root)[0]
                for index in range(count):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, index)) as item:
                            display = _reg_value(item, "DisplayName")
                            candidates: list[Path] = []
                            for value_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                raw = _reg_value(item, value_name)
                                if not raw:
                                    continue
                                # DisplayIcon 可能是 "xxx.exe,0"
                                raw = raw.strip().strip('"').split(",")[0].strip()
                                candidates.extend(_as_paths(raw) or ([Path(raw)] if raw else []))
                            derived = [r for r in (_root_from(c) for c in candidates) if r]
                            if not derived:
                                continue
                            # 定制版 DisplayName 可能不含 mumu：只要路径里真有 MuMu 布局也收。
                            if "mumu" in display.lower() or "网易模拟器" in display:
                                roots.extend(derived)
                            elif any(_looks_like_root(r) for r in derived):
                                roots.extend(derived)
                    except OSError:
                        continue
        except OSError:
            continue

    app_paths = (
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\MuMuPlayer.exe"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\MuMuPlayer.exe"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\MuMuManager.exe"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\MuMuNxDevice.exe"),
    )
    for hive, key_name in app_paths:
        try:
            with winreg.OpenKey(hive, key_name) as item:
                raw = _reg_value(item, "")
        except OSError:
            continue
        for candidate in _as_paths(raw):
            root = _root_from(candidate)
            if root is not None:
                roots.append(root)

    netease_keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Netease"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Netease"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Netease"),
    )
    for hive, key_name in netease_keys:
        try:
            with winreg.OpenKey(hive, key_name) as base:
                for value_index in range(winreg.QueryInfoKey(base)[1]):
                    try:
                        name, raw, _kind = winreg.EnumValue(base, value_index)
                    except OSError:
                        continue
                    if str(name).lower() not in ("installpath", "installlocation", "path"):
                        continue
                    for candidate in _as_paths(str(raw)):
                        root = _root_from(candidate)
                        if root is not None:
                            roots.append(root)
                for sub_index in range(winreg.QueryInfoKey(base)[0]):
                    try:
                        with winreg.OpenKey(base, winreg.EnumKey(base, sub_index)) as sub:
                            for value_name in ("InstallPath", "InstallLocation", "Path"):
                                for candidate in _as_paths(_reg_value(sub, value_name)):
                                    root = _root_from(candidate)
                                    if root is not None:
                                        roots.append(root)
                    except OSError:
                        continue
        except OSError:
            continue
    return roots


def registry_roots() -> list[Path]:
    cached = _cached("registry")
    if cached is not None:
        return list(cached)
    return list(_store("registry", _dedupe_existing(_registry_roots_uncached())))


def _name_roots() -> list[Path]:
    values: list[Path] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if not _is_dir(drive):
            continue
        for name in INSTALL_NAMES:
            values.extend((
                drive / name,
                drive / "Netease" / name,
                drive / "software" / name,
                drive / "Program Files" / "Netease" / name,
                drive / "Program Files (x86)" / "Netease" / name,
            ))
        try:
            values.extend(drive.glob("MuMu*"))
        except OSError:
            pass
        for base in (
            drive / "Netease",
            drive / "software",
            drive / "Program Files" / "Netease",
            drive / "Program Files (x86)" / "Netease",
        ):
            try:
                values.extend(path for path in base.glob("*") if "mumu" in path.name.lower())
            except OSError:
                pass
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if not base:
            continue
        for name in INSTALL_NAMES:
            values.extend((Path(base) / name, Path(base) / "Netease" / name))
    return [root for root in (_root_from(value) for value in values)
            if root is not None and _looks_like_root(root)]


def _layout_scan_roots() -> list[Path]:
    """不看目录名的布局扫描：盘符顶层 + Program Files 下一层。"""
    roots: list[Path] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if not _is_dir(drive):
            continue
        try:
            children = list(drive.iterdir())[:MAX_CHILDREN]
        except OSError:
            continue
        for child in children:
            if child.name.lower() in WALK_SKIP_DIRS:
                continue
            if _looks_like_root(child):
                roots.append(child)
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if not base:
            continue
        try:
            children = list(Path(base).iterdir())[:MAX_CHILDREN]
        except OSError:
            continue
        for child in children:
            if not _is_dir(child):
                continue
            if _looks_like_root(child):
                roots.append(child)
                continue
            try:
                grandchildren = list(child.iterdir())[:MAX_CHILDREN]
            except OSError:
                continue
            for grandchild in grandchildren:
                if _is_dir(grandchild) and _looks_like_root(grandchild):
                    roots.append(grandchild)
    return roots


def _common_roots() -> list[Path]:
    cached = _cached("common")
    if cached is not None:
        return list(cached)
    return list(_store("common", _dedupe_existing(_name_roots() + _layout_scan_roots())))


def _running_process_paths() -> list[Path]:
    """枚举进程 exe 绝对路径（stdlib ctypes；非 Windows 返回空）。"""
    if os.name != "nt":
        return []
    cached = _cached("process_paths")
    if cached is not None:
        return list(cached)

    paths: list[Path] = []
    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        psapi.EnumProcesses.argtypes = (
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        psapi.EnumProcesses.restype = wintypes.BOOL

        capacity = 2048
        while capacity <= 16384:
            array = (wintypes.DWORD * capacity)()
            needed = wintypes.DWORD()
            if not psapi.EnumProcesses(array, ctypes.sizeof(array), ctypes.byref(needed)):
                break
            count = needed.value // ctypes.sizeof(wintypes.DWORD)
            if count >= capacity:
                capacity *= 2
                continue
            for index in range(count):
                pid = array[index]
                if not pid:
                    continue
                handle = kernel32.OpenProcess(
                    process_query_limited_information, False, pid)
                if not handle:
                    continue
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buffer))
                    if kernel32.QueryFullProcessImageNameW(
                            handle, 0, buffer, ctypes.byref(size)):
                        paths.append(Path(buffer.value))
                finally:
                    kernel32.CloseHandle(handle)
            break
    except Exception:  # noqa: BLE001  进程枚举只是加分项，失败不阻断
        paths = []
    return list(_store("process_paths", paths))


def _process_roots() -> list[Path]:
    cached = _cached("process_roots")
    if cached is not None:
        return list(cached)
    roots: list[Path] = []
    for exe in _running_process_paths():
        try:
            for parent in (exe.parent, *list(exe.parents)[:4]):
                if _looks_like_root(parent):
                    roots.append(parent)
                    break
        except OSError:
            continue
    return list(_store("process_roots", _dedupe_existing(roots)))


def _dedupe_existing(values) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        root = _root_from(value)
        if root is None or not _is_dir(root):
            continue
        key = str(root).lower().rstrip("\\/")
        if key in seen:
            continue
        seen.add(key)
        result.append(root)
    return result


def _candidate_roots(project_root: Path, preferred=()) -> list[tuple[Path, bool]]:
    """按优先级返回 (根目录, 是否允许深度搜索)。"""
    pairs: list[tuple[Path, bool]] = []

    def add(value, deep: bool) -> None:
        root = _root_from(value)
        if root is None or not _is_dir(root):
            return
        pairs.append((root, deep))

    def add_anchor(value) -> None:
        """显式锚点：本目录深度搜索；认不出布局时才向上两级兜底。

        认出布局的锚点（``\\shell\\MuMuPlayer.exe``、``\\nx_main\\adb.exe`` 等）
        已归一到安装根，adb 必在本根内，无需再扫上级目录（避免把无关工程目录
        整棵走一遍）。
        """
        root = _root_from(value)
        if root is None or not _is_dir(root):
            return
        pairs.append((root, True))
        if _looks_like_root(root):
            return
        try:
            for parent in list(root.parents)[:2]:
                if parent.parent == parent:  # 盘符根不做深度搜索
                    break
                pairs.append((parent, True))
        except OSError:
            pass

    for value in (
        os.environ.get("MUMU_HOME"),
        os.environ.get("MUMU_ROOT"),
        *preferred,
    ):
        add_anchor(value)
    for value in configured_paths(project_root):
        add_anchor(value)
    add(_read_json(project_root / RUNTIME_CACHE).get("root"), True)
    for root in _process_roots():
        add(root, True)
    for root in registry_roots():
        add(root, True)
    for root in _common_roots():
        add(root, True)

    result: list[tuple[Path, bool]] = []
    seen: set[str] = set()
    for root, deep in pairs:
        key = str(root).lower().rstrip("\\/")
        if key in seen:
            continue
        seen.add(key)
        result.append((root, deep))
    return result


def candidate_roots(project_root: Path, preferred=()) -> list[Path]:
    """按优先级给出候选根目录（已归一并去重，仅保留真实存在的目录）。"""
    return [root for root, _deep in _candidate_roots(project_root, preferred)]


def _adb_in_layouts(root: Path) -> Path | None:
    # 与 scripts/shell_device_binding.py 的顺序一致：优先 nx_device 自带 adb。
    # MaaToolkit 枚举出的 Name 用的也是这个路径；写错 adb 路径会直接导致指纹 Name 不匹配。
    try:
        for candidate in sorted(root.glob("nx_device/*/shell/adb.exe"), reverse=True):
            if _is_file(candidate):
                return candidate
    except OSError:
        pass
    for relative in ADB_LAYOUTS:
        candidate = root / relative
        if _is_file(candidate):
            return candidate
    return None


def _manager_in_layouts(root: Path) -> Path | None:
    for relative in MANAGER_LAYOUTS:
        candidate = root / relative
        if _is_file(candidate):
            return candidate
    return None


def _walk_root(root: Path) -> tuple[Path | None, Path | None]:
    """有界深度搜索 adb 与 MuMuManager；用于布局不标准的定制安装。"""
    adb: Path | None = None
    manager: Path | None = None
    base_depth = len(root.parts)
    seen_files = 0
    try:
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.parts) - base_depth
            dirs[:] = [name for name in dirs if name.lower() not in WALK_SKIP_DIRS]
            if depth >= WALK_DEPTH:
                dirs[:] = []
            for name in files:
                seen_files += 1
                lowered = name.lower()
                if adb is None and lowered in ADB_NAMES:
                    adb = current_path / name
                elif manager is None and lowered == "mumumanager.exe":
                    manager = current_path / name
                if adb is not None and manager is not None:
                    return adb, manager
            if seen_files > MAX_WALK_FILES:
                break
    except OSError:
        pass
    return adb, manager


def _probe_root(root: Path, deep: bool = True) -> tuple[Path | None, Path | None]:
    key = f"probe:{str(root).lower()}:{1 if deep else 0}"
    cached = _cached(key)
    if cached is not None:
        return cached
    adb = _adb_in_layouts(root)
    manager = _manager_in_layouts(root)
    if deep and (adb is None or manager is None) and root.parent != root:
        walked_adb, walked_manager = _walk_root(root)
        adb = adb or walked_adb
        manager = manager or walked_manager
    return _store(key, (adb, manager))


def external_platform_tools() -> list[Path]:
    """模拟器之外的 platform-tools（Android SDK / 工程目录自带）。"""
    values: list[Path] = []
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        base = os.environ.get(key, "").strip()
        if base:
            values.append(Path(base) / "platform-tools" / "adb.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        values.append(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    profile = os.environ.get("USERPROFILE")
    if profile:
        values.append(
            Path(profile) / "AppData" / "Local" / "Android" / "Sdk"
            / "platform-tools" / "adb.exe"
        )
    return [path for path in values if _is_file(path)]


def fix_hint() -> list[str]:
    """失败时给普通用户的可执行指引（壳设置优先，其次环境变量）。"""
    return [
        "怎么修（任选其一，填完重跑任务即可）：",
        "  1. 壳首页「当前控制器」为 ADB 时，点「自定义」打开 Adb 编辑器，"
        "在「Adb路径」填 adb.exe 的完整路径"
        "（例如 D:\\MuMu\\nx_main\\adb.exe，不能只写 adb）；",
        "  2. 设置环境变量 MUMU_ADB=<adb.exe 完整路径>；",
        "  3. 只知道安装目录时，设置 MUMU_HOME=<MuMu 安装根目录>，"
        "本工具会在该目录及其下一层自动找 adb。",
        "  填完若仍失败，把本段日志（已检查的位置）一起反馈。",
        "  本任务不会改用裸 adb，也不会擅自替换成其他版本的 adb。",
    ]


def find_adb(root: Path) -> Path | None:
    explicit = os.environ.get("MUMU_ADB", "").strip()
    if explicit:
        path = Path(explicit)
        if _is_file(path):
            return path
    root = Path(root)
    adb, _manager = _probe_root(root, deep=root.parent != root)
    return adb


def find_manager(root: Path) -> Path | None:
    """返回安装根下的 MuMuManager.exe（兼容 shell\\ 布局）；找不到为 None。"""
    _adb, manager = _probe_root(Path(root), deep=Path(root).parent != Path(root))
    return manager


def find_mumu(project_root: Path, preferred=()):
    """返回 (root, MuMuManager.exe, adb.exe)；找不到的项为 None。

    adb 与 manager 独立解析：定制安装没有 ``nx_main\\MuMuManager.exe`` 时，
    只要同一安装根下能找到 adb，仍然返回 adb，避免整条链路因管理器缺失停摆。
    """
    explicit_manager = os.environ.get("MUMU_MANAGER", "").strip()
    if explicit_manager:
        manager = Path(explicit_manager)
        if _is_file(manager):
            root = _root_from(manager) or manager.parent
            adb, _ = _probe_root(root, deep=True)
            return root, manager, adb

    manager_only = None
    for root, deep in _candidate_roots(project_root, preferred):
        adb, manager = _probe_root(root, deep)
        if adb:
            return (
                root.resolve(),
                manager.resolve() if manager else None,
                adb.resolve(),
            )
        if manager and manager_only is None:
            manager_only = (root.resolve(), manager.resolve(), None)
    if manager_only is not None:
        return manager_only
    return None, None, None


def resolve_adb(project_root: Path, configured="") -> str:
    """解析可执行 adb 的绝对路径；全部落空抛 MuMuNotFound（不回落到裸 adb）。"""
    for key in ("ADB_PATH", "MUMU_ADB"):
        value = os.environ.get(key, "").strip()
        if value:
            path = Path(value)
            if _is_file(path):
                return str(path)

    text = str(configured or "").strip()
    if text and text.lower() != "adb":
        for path in _as_paths(text) or [Path(text)]:
            if _is_file(path):
                return str(path)
        root = _root_from(text)
        if root is not None:
            adb = find_adb(root)
            if adb:
                return str(adb)

    _root, _manager, adb = find_mumu(project_root)
    if adb:
        return str(adb)

    # 模拟器本体没带 adb 时，最后才用外部 platform-tools（仍是绝对路径）。
    external = external_platform_tools()
    if external:
        return str(external[0])

    found = shutil.which("adb")
    if found:
        return found
    raise MuMuNotFound(not_found_message(project_root))


def describe_search(project_root: Path) -> list[str]:
    """列出定位时查过的位置与每个候选的探测结果，供失败日志使用。"""
    configured = configured_paths(project_root)
    registry = registry_roots()
    process = _process_roots()
    pairs = _candidate_roots(project_root)
    roots = [root for root, _deep in pairs]
    lines = [
        "环境变量：MUMU_HOME={}、MUMU_ROOT={}、MUMU_MANAGER={}、MUMU_ADB={}、ADB_PATH={}".format(
            os.environ.get("MUMU_HOME") or "无",
            os.environ.get("MUMU_ROOT") or "无",
            os.environ.get("MUMU_MANAGER") or "无",
            os.environ.get("MUMU_ADB") or "无",
            os.environ.get("ADB_PATH") or "无",
        ),
        "实例配置锚点：" + ("、".join(str(path) for path in configured) or "无"),
        "运行进程锚点：" + ("、".join(str(path) for path in process) or "无"),
        "注册表：" + ("、".join(str(path) for path in registry) or "无"),
        "候选安装目录（共 {}）：".format(len(roots))
        + ("、".join(str(path) for path in roots[:24]) or "无"),
    ]
    if len(roots) > 24:
        lines.append(f"（其余 {len(roots) - 24} 个候选目录已省略）")
    if pairs:
        lines.append("前 12 个候选的布局探测：")
        for root, deep in pairs[:12]:
            adb, manager = _probe_root(root, deep)
            lines.append(
                f"  - {root} → adb={adb or '无'} / manager={manager or '无'}"
            )
    lines.append("外部 platform-tools："
                 + ("、".join(str(path) for path in external_platform_tools()) or "无"))
    lines.append("PATH 中的 adb：" + (shutil.which("adb") or "无"))
    lines.extend(fix_hint())
    return lines


def not_found_message(project_root: Path) -> str:
    body = "\n".join("  - " + line for line in describe_search(project_root))
    return "未找到可用的 adb.exe（不回落到裸 adb）。已检查：\n" + body
