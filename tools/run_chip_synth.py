"""芯片合成干跑/真合入口（不经过壳，直连 ADB）。

用法：
  python tools/run_chip_synth.py --address 127.0.0.1:16416            # 干跑
  LAA_CHIP_SYNTH_REAL=1 python tools/run_chip_synth.py --address ...  # 真合

选项由环境变量传入：LAA_CHIP_SYNTH_SKILLS=连击,神力 / LAA_CHIP_SYNTH_QUALITIES=R5 /
LAA_CHIP_SYNTH_THRESHOLD=2/1/1/1。
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install"
os.environ.setdefault("MAAFW_BINARY_PATH", str(INSTALL / "runtimes" / "win-x64" / "native"))
os.environ.setdefault("TEMP", str(ROOT / ".tmp" / "chip_synth"))
os.environ.setdefault("TMP", os.environ["TEMP"])
sys.path.insert(0, str(ROOT / "agent"))

from maa.controller import AdbController
from maa.context import ContextEventSink
from maa.resource import Resource
from maa.tasker import Tasker

from chip_synth import ChipSynthAction, ChipSynthRecognition


class DiagnosticSink(ContextEventSink):
    def on_raw_notification(self, context, msg, details):
        if msg == "Node.PipelineNode.Failed":
            name = details.get("name") or details.get("entry") or ""
            print(f"MAA_EVENT {msg} {name}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", default=r"D:\Program Files\Netease\MuMu\nx_device\12.0\shell\adb.exe")
    parser.add_argument("--address", default="127.0.0.1:16416")
    args = parser.parse_args()

    temp_dir = Path(os.environ["TEMP"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(temp_dir / "maa-log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    resource = Resource()
    if not resource.post_bundle(INSTALL / "resource").wait().succeeded:
        raise RuntimeError("Failed to load Maa resource bundle")
    if not resource.register_custom_action("chip_synth", ChipSynthAction()):
        raise RuntimeError("Failed to register chip_synth action")
    if not resource.register_custom_recognition("chip_synth_state", ChipSynthRecognition()):
        raise RuntimeError("Failed to register chip_synth_state recognition")

    controller = AdbController(
        args.adb,
        args.address,
        agent_path=INSTALL / "libs" / "MaaAgentBinary",
    )
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError("Failed to connect MuMu ADB controller")

    tasker = Tasker()
    if not tasker.bind(resource, controller) or not tasker.inited:
        raise RuntimeError("Failed to initialize Maa tasker")
    tasker.add_context_sink(DiagnosticSink())
    job = tasker.post_task("芯片合成").wait()
    print(
        "CHIP_SYNTH_STATUS",
        f"succeeded={job.succeeded}",
        f"failed={job.status.failed}",
        f"raw={job.status._status.name}",
    )
    return 0 if job.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
