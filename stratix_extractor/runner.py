"""Run a whole extraction: each selected switch in turn, then the workbook."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .collector import Collector, LogFn, SwitchResult
from .excel_writer import RunInfo, verify_workbook, write_workbook
from .switchlist import Credentials, SwitchTarget, resolve_credentials


@dataclass
class RunReport:
    results: list[SwitchResult] = field(default_factory=list)
    output_path: Path | None = None
    counts: dict[str, int] = field(default_factory=dict)
    verified: bool = False
    verify_message: str = ""
    write_error: str = ""
    stopped: bool = False

    @property
    def succeeded(self) -> list[SwitchResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[SwitchResult]:
        return [r for r in self.results if not r.success]


def run_extraction(
    targets: list[SwitchTarget],
    defaults: Credentials,
    override_credentials: bool,
    output_path: str | Path,
    input_file: str,
    log: LogFn,
    collector: Collector,
    stop_event: threading.Event | None = None,
    on_switch_start: Callable[[int, SwitchTarget], None] | None = None,
    on_switch_done: Callable[[int, SwitchResult], None] | None = None,
) -> RunReport:
    report = RunReport()
    stop_event = stop_event or threading.Event()
    started = datetime.now()
    total = len(targets)
    log(f"Starting extraction of {total} switch{'es' if total != 1 else ''} at "
        f"{started:%Y-%m-%d %H:%M:%S}.", "info")

    for index, target in enumerate(targets):
        if stop_event.is_set():
            report.stopped = True
            log(f"Stopped by user before {target.label}; {total - index} switch(es) not attempted.", "warn")
            break
        log("", "info")
        log(f"[{index + 1}/{total}] {target.label}", "heading")
        if on_switch_start:
            on_switch_start(index, target)
        creds = resolve_credentials(target, defaults, override_credentials)
        result = collector.collect(target, creds)
        report.results.append(result)
        if on_switch_done:
            on_switch_done(index, result)

    finished = datetime.now()
    log("", "info")
    if not report.results:
        log("No switches were attempted, so no spreadsheet was written.", "warn")
        return report

    path = Path(output_path)
    info = RunInfo(started=started, finished=finished, input_file=input_file, selected=total)
    log(f"Writing spreadsheet {path} ...", "info")
    try:
        report.counts = write_workbook(path, report.results, info)
    except PermissionError:
        # File was opened in Excel during the run: save alongside instead.
        alternative = path.with_name(f"{path.stem}_{finished:%Y%m%d_%H%M%S}{path.suffix}")
        log(f"{path.name} is locked (open in Excel?). Saving as {alternative.name} instead.", "warn")
        path = alternative
        try:
            report.counts = write_workbook(path, report.results, info)
        except Exception as exc:
            report.write_error = f"Could not write the spreadsheet: {exc}"
            log(report.write_error, "error")
            return report
    except Exception as exc:
        report.write_error = f"Could not write the spreadsheet: {exc}"
        log(report.write_error, "error")
        return report

    report.output_path = path
    report.verified, report.verify_message = verify_workbook(path, report.counts)
    if report.verified:
        log(f"Spreadsheet saved and verified: {report.verify_message} rows.", "ok")
    else:
        log(f"Spreadsheet saved but verification found problems: {report.verify_message}", "error")

    ok, bad = len(report.succeeded), len(report.failed)
    log(f"Finished at {finished:%Y-%m-%d %H:%M:%S}: {ok} succeeded, {bad} failed"
        f"{', stopped early' if report.stopped else ''}.", "ok" if bad == 0 else "warn")
    for result in report.failed:
        log(f"  Failed: {result.target.label} - {result.error}", "error")
    return report
