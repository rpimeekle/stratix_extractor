"""Entry point: `python -m stratix_extractor` opens the GUI.

`--self-test RESULT_FILE` checks that the bundled libraries load and that a
workbook can be written, then writes the outcome to RESULT_FILE. The Windows
build uses it to test the packaged .exe, which has no console to print to.
"""

from __future__ import annotations

import sys


def self_test(result_file: str) -> int:
    lines = []
    try:
        import tempfile
        from pathlib import Path

        import netmiko
        import paramiko
        import openpyxl
        import tkinter

        from . import __version__
        from .collector import Collector, SwitchResult, build_result
        from .excel_writer import verify_workbook, write_workbook, RunInfo
        from .switchlist import SwitchTarget
        from datetime import datetime

        lines.append(f"stratix-extractor {__version__}")
        lines.append(f"netmiko {netmiko.__version__}, paramiko {paramiko.__version__}, "
                     f"openpyxl {openpyxl.__version__}, Tk {tkinter.TkVersion}")
        result = SwitchResult(target=SwitchTarget("SELFTEST", "192.0.2.1"), started=datetime.now(), success=True)
        result.raw = {"interfaces": "GigabitEthernet1/1 is up, line protocol is up (connected)\n"
                                    "  Description: self test port with a long description\n"}
        build_result(result)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "selftest.xlsx"
            counts = write_workbook(path, [result], RunInfo(datetime.now(), datetime.now(), "-", 1))
            ok, message = verify_workbook(path, counts)
        if not ok:
            raise RuntimeError(f"workbook verification failed: {message}")
        Collector(lambda m, l: None)  # constructs without connecting
        lines.append(f"workbook ok: {message}")
        lines.append("SELF-TEST PASSED")
        code = 0
    except Exception:  # report anything that breaks in the bundle
        import traceback

        lines.append("SELF-TEST FAILED")
        lines.append(traceback.format_exc())
        code = 1
    with open(result_file, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return code


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        sys.exit(self_test(sys.argv[2]))
    from .gui import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
