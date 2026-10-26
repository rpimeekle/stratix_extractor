"""Tkinter front end."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import APP_NAME, __version__
from .collector import Collector
from .excel_writer import check_writable
from .runner import RunReport, run_extraction
from .switchlist import Credentials, SwitchTarget, load_switch_list, resolve_credentials, write_template

TICK, UNTICK = "☑", "☐"
LOG_COLOURS = {
    "ok": "#1b7f2a",
    "warn": "#b35c00",
    "error": "#c0181f",
    "detail": "#5f6368",
    "info": None,
    "heading": None,
}


def _settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "StratixExtractor" / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    # Passwords are never stored.
    try:
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def default_output_name() -> str:
    return f"stratix_extract_{datetime.now():%Y%m%d_%H%M}.xlsx"


class StratixExtractorApp:
    def __init__(self, root: tk.Tk, collector_factory=None, settings: dict | None = None):
        self.root = root
        self.collector_factory = collector_factory or (
            lambda log, timeout, raw_dir: Collector(log, timeout=timeout, raw_dir=raw_dir)
        )
        self.settings = load_settings() if settings is None else settings
        self.messages: queue.Queue = queue.Queue()
        self.targets: list[SwitchTarget] = []
        self.checked: set[str] = set()
        self.running = False
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.run_ids: list[str] = []
        self.last_report: RunReport | None = None

        root.title(f"{APP_NAME} {__version__}")
        root.geometry(self.settings.get("geometry", "1120x800"))
        root.minsize(900, 620)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._callback_error

        self._build()
        self.root.after(100, self._poll)

        last_input = self.settings.get("input_file", "")
        if last_input and Path(last_input).exists():
            self.load_list(last_input, quiet=True)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        style = ttk.Style(self.root)
        body_font = tkfont.nametofont("TkDefaultFont")
        style.configure("Treeview", rowheight=int(body_font.metrics("linespace") * 1.5))
        style.configure("Go.TButton", font=(body_font.actual("family"), body_font.actual("size"), "bold"))

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # Files
        files = ttk.LabelFrame(outer, text="Files", padding=8)
        files.grid(row=0, column=0, sticky="ew")
        files.columnconfigure(1, weight=1)

        self.input_var = tk.StringVar(value=self.settings.get("input_file", ""))
        ttk.Label(files, text="Switch list (CSV/XLSX):").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.input_entry = ttk.Entry(files, textvariable=self.input_var)
        self.input_entry.grid(row=0, column=1, sticky="ew")
        self.input_entry.bind("<Return>", lambda e: self.load_list(self.input_var.get()))
        self.input_btn = ttk.Button(files, text="Browse…", command=self.browse_input)
        self.input_btn.grid(row=0, column=2, padx=(6, 0))
        self.reload_btn = ttk.Button(files, text="Reload", command=lambda: self.load_list(self.input_var.get()))
        self.reload_btn.grid(row=0, column=3, padx=(6, 0))
        self.template_btn = ttk.Button(files, text="New template…", command=self.save_template)
        self.template_btn.grid(row=0, column=4, padx=(6, 0))

        out_dir = self.settings.get("output_dir") or str(Path.home() / "Documents")
        self.output_var = tk.StringVar(value=str(Path(out_dir) / default_output_name()))
        ttk.Label(files, text="Output spreadsheet:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        self.output_entry = ttk.Entry(files, textvariable=self.output_var)
        self.output_entry.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.output_btn = ttk.Button(files, text="Browse…", command=self.browse_output)
        self.output_btn.grid(row=1, column=2, padx=(6, 0), pady=(6, 0))
        self.newname_btn = ttk.Button(files, text="New name", command=self.new_output_name)
        self.newname_btn.grid(row=1, column=3, padx=(6, 0), pady=(6, 0))

        opts = ttk.Frame(files)
        opts.grid(row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.raw_var = tk.BooleanVar(value=self.settings.get("save_raw", False))
        self.raw_check = ttk.Checkbutton(
            opts, text="Also save raw command output (text files beside the spreadsheet)",
            variable=self.raw_var)
        self.raw_check.pack(side="left")
        ttk.Label(opts, text="SSH timeout (s):").pack(side="left", padx=(20, 4))
        self.timeout_var = tk.IntVar(value=int(self.settings.get("timeout", 20)))
        self.timeout_spin = ttk.Spinbox(opts, from_=5, to=180, width=5, textvariable=self.timeout_var)
        self.timeout_spin.pack(side="left")

        # Credentials
        creds = ttk.LabelFrame(outer, text="Default credentials", padding=8)
        creds.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.user_var = tk.StringVar(value=self.settings.get("username", ""))
        self.pass_var = tk.StringVar()
        self.secret_var = tk.StringVar()
        self.override_var = tk.BooleanVar(value=self.settings.get("override", False))
        ttk.Label(creds, text="Username:").grid(row=0, column=0, sticky="w")
        self.user_entry = ttk.Entry(creds, textvariable=self.user_var, width=20)
        self.user_entry.grid(row=0, column=1, padx=(4, 16))
        ttk.Label(creds, text="Password:").grid(row=0, column=2, sticky="w")
        self.pass_entry = ttk.Entry(creds, textvariable=self.pass_var, show="•", width=20)
        self.pass_entry.grid(row=0, column=3, padx=(4, 16))
        ttk.Label(creds, text="Enable secret (optional):").grid(row=0, column=4, sticky="w")
        self.secret_entry = ttk.Entry(creds, textvariable=self.secret_var, show="•", width=20)
        self.secret_entry.grid(row=0, column=5, padx=(4, 0))
        self.override_check = ttk.Checkbutton(
            creds, text="Use these for every switch (ignore credentials in the list)",
            variable=self.override_var, command=self._refresh_credential_column)
        self.override_check.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(creds, text="Otherwise used only where the list leaves the username or password blank.",
                  foreground="#5f6368").grid(row=1, column=4, columnspan=2, sticky="w", pady=(6, 0))

        # Switch list + log in a resizable split
        panes = ttk.PanedWindow(outer, orient="vertical")
        panes.grid(row=2, column=0, sticky="nsew", pady=(8, 0))

        listframe = ttk.LabelFrame(panes, text="Switches (click a row to select or unselect it)", padding=8)
        listframe.columnconfigure(0, weight=1)
        listframe.rowconfigure(0, weight=1)
        columns = ("sel", "hostname", "ip", "port", "creds", "result")
        self.tree = ttk.Treeview(listframe, columns=columns, show="headings", selectmode="none", height=8)
        for col, text, width, anchor, stretch in (
            ("sel", "", 36, "center", False),
            ("hostname", "Hostname", 200, "w", True),
            ("ip", "IP address", 140, "w", False),
            ("port", "SSH port", 70, "center", False),
            ("creds", "Credentials", 150, "w", False),
            ("result", "Result", 360, "w", True),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch)
        self.tree.heading("sel", text=TICK, command=self.toggle_all)
        self.tree.tag_configure("ok", foreground=LOG_COLOURS["ok"])
        self.tree.tag_configure("failed", foreground=LOG_COLOURS["error"])
        self.tree.tag_configure("active", background="#fff4cc")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(listframe, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        listbar = ttk.Frame(listframe)
        listbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.select_all_btn = ttk.Button(listbar, text="Select all", command=self.select_all)
        self.select_all_btn.pack(side="left")
        self.reset_btn = ttk.Button(listbar, text="Reset", command=self.reset_selection)
        self.reset_btn.pack(side="left", padx=(6, 0))
        self.count_var = tk.StringVar(value="No switch list loaded.")
        ttk.Label(listbar, textvariable=self.count_var).pack(side="left", padx=(12, 0))
        panes.add(listframe, weight=2)

        logframe = ttk.LabelFrame(panes, text="Progress", padding=8)
        logframe.columnconfigure(0, weight=1)
        logframe.rowconfigure(1, weight=1)
        actions = ttk.Frame(logframe)
        actions.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.go_btn = ttk.Button(actions, text="Go", style="Go.TButton", width=10, command=self.start_run)
        self.go_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", width=10, command=self.request_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(actions, mode="determinate", length=220)
        self.progress.pack(side="left", padx=(12, 0))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(actions, textvariable=self.status_var).pack(side="left", padx=(12, 0))
        ttk.Button(actions, text="Save log…", command=self.save_log).pack(side="right")
        ttk.Button(actions, text="Clear log", command=self.clear_log).pack(side="right", padx=(0, 6))
        self.open_btn = ttk.Button(actions, text="Open spreadsheet", command=self.open_output, state="disabled")
        self.open_btn.pack(side="right", padx=(0, 6))

        mono = tkfont.nametofont("TkFixedFont").copy()
        self.log_text = tk.Text(logframe, height=14, wrap="word", font=mono, state="disabled",
                                relief="flat", background="#fbfbfb", padx=6, pady=4)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(logframe, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        for level, colour in LOG_COLOURS.items():
            if colour:
                self.log_text.tag_configure(level, foreground=colour)
        bold = mono.copy()
        bold.configure(weight="bold")
        self.log_text.tag_configure("heading", font=bold)
        self._bold_font = bold
        panes.add(logframe, weight=3)

        self.inputs = [self.input_entry, self.input_btn, self.reload_btn, self.template_btn,
                       self.output_entry, self.output_btn, self.newname_btn, self.raw_check,
                       self.timeout_spin, self.user_entry, self.pass_entry, self.secret_entry,
                       self.override_check, self.select_all_btn, self.reset_btn, self.go_btn]

    # ------------------------------------------------------------- logging
    def log(self, message: str, level: str = "info") -> None:
        """Append a line to the log. Main thread only; workers use self.messages."""
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        if message:
            self.log_text.insert("end", f"{stamp}  ", "detail")
            self.log_text.insert("end", message + "\n", level)
        else:
            self.log_text.insert("end", "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save log", defaultextension=".txt",
            initialfile=f"stratix_extract_log_{datetime.now():%Y%m%d_%H%M}.txt",
            filetypes=[("Text file", "*.txt")])
        if path:
            Path(path).write_text(self.log_text.get("1.0", "end"), encoding="utf-8")
            self.log(f"Log saved to {path}", "ok")

    # --------------------------------------------------------------- files
    def browse_input(self) -> None:
        initial = Path(self.input_var.get()).parent if self.input_var.get() else Path.home()
        path = filedialog.askopenfilename(
            parent=self.root, title="Select switch list", initialdir=str(initial),
            filetypes=[("Switch lists", "*.csv *.xlsx *.xlsm"), ("CSV", "*.csv"),
                       ("Excel workbook", "*.xlsx *.xlsm"), ("All files", "*.*")])
        if path:
            self.input_var.set(path)
            self.load_list(path)

    def browse_output(self) -> None:
        current = Path(self.output_var.get() or default_output_name())
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save spreadsheet as", defaultextension=".xlsx",
            initialdir=str(current.parent if current.parent.exists() else Path.home()),
            initialfile=current.name, filetypes=[("Excel workbook", "*.xlsx")])
        if path:
            self.output_var.set(path)

    def new_output_name(self) -> None:
        current = Path(self.output_var.get()) if self.output_var.get() else Path.home() / "x"
        folder = current.parent if current.parent.exists() else Path.home()
        self.output_var.set(str(folder / default_output_name()))

    def save_template(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save switch list template", defaultextension=".csv",
            initialfile="stratix_switches.csv",
            filetypes=[("CSV", "*.csv"), ("Excel workbook", "*.xlsx")])
        if not path:
            return
        try:
            write_template(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save the template:\n{exc}", parent=self.root)
            return
        self.log(f"Template saved to {path}. Fill it in, save it, then load it here.", "ok")
        if messagebox.askyesno(APP_NAME, f"Template saved:\n{path}\n\nOpen it now to edit?", parent=self.root):
            open_path(Path(path))

    def load_list(self, path: str, quiet: bool = False) -> bool:
        path = (path or "").strip().strip('"')
        if not path:
            messagebox.showwarning(APP_NAME, "Choose a switch list file first.", parent=self.root)
            return False
        try:
            targets, warnings = load_switch_list(path)
        except Exception as exc:
            self.log(f"Could not load {path}: {exc}", "error")
            if not quiet:
                messagebox.showerror(APP_NAME, f"Could not load the switch list:\n{exc}", parent=self.root)
            return False
        self.targets = targets
        self.input_var.set(path)
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        for index, target in enumerate(targets):
            iid = str(index)
            self.tree.insert("", "end", iid=iid, values=(
                TICK, target.hostname, target.ip, target.port, "", ""))
            self.checked.add(iid)
        self._refresh_credential_column()
        self._update_count()
        self.log(f"Loaded {len(targets)} switches from {path}.", "ok")
        for warning in warnings:
            self.log("  " + warning, "warn")
        if warnings and not quiet:
            self.status_var.set(f"Loaded with {len(warnings)} warning(s) - see the log.")
        else:
            self.status_var.set("Ready.")
        # Default the output to the same folder as the list on first use.
        if not self.settings.get("output_dir"):
            self.output_var.set(str(Path(path).parent / default_output_name()))
        return True

    # -------------------------------------------------------------- list
    def _refresh_credential_column(self) -> None:
        override = self.override_var.get()
        for index, target in enumerate(self.targets):
            if override:
                source = "Default (override)"
            elif target.username and target.password:
                source = "From list"
            elif target.username or target.password:
                source = "List + default"
            else:
                source = "Default"
            self.tree.set(str(index), "creds", source)

    def _on_tree_click(self, event) -> None:
        if self.running:
            return
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._set_checked(iid, iid not in self.checked)
            self._update_count()

    def _set_checked(self, iid: str, value: bool) -> None:
        if value:
            self.checked.add(iid)
        else:
            self.checked.discard(iid)
        self.tree.set(iid, "sel", TICK if value else UNTICK)

    def select_all(self) -> None:
        for iid in self.tree.get_children():
            self._set_checked(iid, True)
        self._update_count()

    def reset_selection(self) -> None:
        """Clear the whole selection and the results of the last run."""
        for iid in self.tree.get_children():
            self._set_checked(iid, False)
            self.tree.set(iid, "result", "")
            self.tree.item(iid, tags=())
        self._update_count()
        self.progress["value"] = 0
        self.status_var.set("Selection cleared.")

    def toggle_all(self) -> None:
        if self.running:
            return
        if len(self.checked) == len(self.tree.get_children()):
            self.reset_selection()
        else:
            self.select_all()

    def _update_count(self) -> None:
        total = len(self.tree.get_children())
        self.count_var.set(f"{len(self.checked)} of {total} selected" if total else "No switch list loaded.")

    def selected_targets(self) -> list[tuple[str, SwitchTarget]]:
        return [(iid, self.targets[int(iid)]) for iid in self.tree.get_children() if iid in self.checked]

    # --------------------------------------------------------------- run
    def _validate(self) -> tuple[list[tuple[str, SwitchTarget]], Path] | None:
        if not self.targets:
            messagebox.showwarning(APP_NAME, "Load a switch list first.", parent=self.root)
            return None
        selected = self.selected_targets()
        if not selected:
            messagebox.showwarning(APP_NAME, "No switches are selected.", parent=self.root)
            return None

        defaults = self._defaults()
        missing = []
        for _, target in selected:
            creds = resolve_credentials(target, defaults, self.override_var.get())
            if not (creds.username and creds.password):
                missing.append(target.label)
        if missing:
            shown = "\n".join(missing[:10]) + ("\n…" if len(missing) > 10 else "")
            messagebox.showwarning(
                APP_NAME,
                "These switches have no username or password in the list, and no default "
                f"credentials are entered:\n\n{shown}", parent=self.root)
            return None

        out_text = self.output_var.get().strip().strip('"')
        if not out_text:
            self.new_output_name()
            out_text = self.output_var.get()
        output = Path(out_text)
        if output.suffix.lower() != ".xlsx":
            output = output.with_suffix(".xlsx")
            self.output_var.set(str(output))
        problem = check_writable(output)
        if problem:
            messagebox.showerror(APP_NAME, problem, parent=self.root)
            return None
        if output.exists() and not messagebox.askyesno(
                APP_NAME, f"{output.name} already exists. Replace it?", parent=self.root):
            return None
        return selected, output

    def _defaults(self) -> Credentials:
        return Credentials(self.user_var.get().strip(), self.pass_var.get(), self.secret_var.get())

    def start_run(self) -> bool:
        if self.running:
            return False
        checked = self._validate()
        if not checked:
            return False
        selected, output = checked
        try:
            timeout = max(5, int(self.timeout_var.get()))
        except (tk.TclError, ValueError):
            timeout = 20
        raw_dir = output.with_name(output.stem + "_raw") if self.raw_var.get() else None

        self._remember(output, timeout)
        self.run_ids = [iid for iid, _ in selected]
        for iid in self.tree.get_children():
            self.tree.item(iid, tags=())
            self.tree.set(iid, "result", "Waiting" if iid in self.checked else "")
        self.progress.configure(maximum=len(selected), value=0)
        self.stop_event.clear()
        self._set_running(True)
        self.status_var.set(f"Running 0 of {len(selected)} …")
        self.log("", "info")

        defaults = self._defaults()
        override = self.override_var.get()
        targets = [t for _, t in selected]
        input_file = self.input_var.get()

        def post_log(message, level="info"):
            self.messages.put(("log", message, level))

        def work():
            try:
                collector = self.collector_factory(post_log, timeout, raw_dir)
                report = run_extraction(
                    targets, defaults, override, output, input_file, post_log, collector,
                    stop_event=self.stop_event,
                    on_switch_start=lambda i, t: self.messages.put(("start", i)),
                    on_switch_done=lambda i, r: self.messages.put(("done", i, r)),
                )
                self.messages.put(("finished", report))
            except Exception:
                self.messages.put(("crashed", traceback.format_exc()))

        self.worker = threading.Thread(target=work, name="extraction", daemon=True)
        self.worker.start()
        return True

    def request_stop(self) -> None:
        if self.running and not self.stop_event.is_set():
            self.stop_event.set()
            self.stop_btn.configure(state="disabled")
            self.status_var.set("Stopping after the current switch …")
            self.log("Stop requested - finishing the current switch, then writing the spreadsheet.", "warn")

    def _set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        for widget in self.inputs:
            widget.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")
        if running:
            self.open_btn.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                message = self.messages.get_nowait()
                kind = message[0]
                if kind == "log":
                    self.log(message[1], message[2])
                elif kind == "start":
                    iid = self.run_ids[message[1]]
                    self.tree.set(iid, "result", "Connecting …")
                    self.tree.item(iid, tags=("active",))
                    self.tree.see(iid)
                    self.status_var.set(f"Running {message[1] + 1} of {len(self.run_ids)} …")
                elif kind == "done":
                    index, result = message[1], message[2]
                    iid = self.run_ids[index]
                    if result.success:
                        d = result.details
                        text = (f"✔ {d.get('Model', '')}  fw {d.get('Firmware version', '')}  "
                                f"{d.get('Ports', 0)} ports")
                        if result.warnings:
                            text += f"  ({len(result.warnings)} warning(s))"
                        self.tree.item(iid, tags=("ok",))
                    else:
                        text = f"✖ {result.error}"
                        self.tree.item(iid, tags=("failed",))
                    self.tree.set(iid, "result", text)
                    self.progress["value"] = index + 1
                elif kind == "finished":
                    self._finish(message[1])
                elif kind == "crashed":
                    self._set_running(False)
                    self.log("Unexpected error:\n" + message[1], "error")
                    self.status_var.set("Stopped by an unexpected error.")
                    messagebox.showerror(APP_NAME, "The run stopped because of an unexpected error. "
                                         "Details are in the log.", parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish(self, report: RunReport) -> None:
        self._set_running(False)
        self.last_report = report
        for iid in self.run_ids[len(report.results):]:
            self.tree.set(iid, "result", "Not attempted (stopped)")
        ok, total = len(report.succeeded), len(report.results)
        if report.output_path:
            self.output_var.set(str(report.output_path))
            self.open_btn.configure(state="normal")
        self.status_var.set(
            f"Done: {ok} of {total} succeeded" + (" (stopped early)" if report.stopped else "") + ".")
        self.confirm(report)

    def confirm(self, report: RunReport) -> None:
        """Final confirmation dialog (overridden in tests)."""
        ok, total = len(report.succeeded), len(report.results)
        lines = []
        if total:
            lines.append(f"Switches extracted successfully: {ok} of {total}.")
        if report.stopped:
            lines.append("The run was stopped before every selected switch was attempted.")
        for result in report.failed[:8]:
            lines.append(f"  ✖ {result.target.label}: {result.error}")
        if len(report.failed) > 8:
            lines.append(f"  … and {len(report.failed) - 8} more (see the Summary sheet).")

        if report.output_path and report.verified:
            lines += ["", "Spreadsheet saved and checked:", str(report.output_path), "",
                      "Rows written: " + report.verify_message + ".", "", "Open the spreadsheet now?"]
            title = "Extraction complete" if ok == total and not report.stopped else "Extraction finished with problems"
            if messagebox.askyesno(title, "\n".join(lines), parent=self.root):
                self.open_output()
        elif report.output_path:
            lines += ["", f"The spreadsheet was saved but did not verify: {report.verify_message}",
                      str(report.output_path)]
            messagebox.showwarning("Spreadsheet problem", "\n".join(lines), parent=self.root)
        elif report.write_error:
            lines += ["", report.write_error]
            messagebox.showerror("Spreadsheet not saved", "\n".join(lines), parent=self.root)
        else:
            messagebox.showinfo(APP_NAME, "No switches were attempted, so no spreadsheet was written.",
                                parent=self.root)

    def open_output(self) -> None:
        if self.last_report and self.last_report.output_path:
            try:
                open_path(self.last_report.output_path)
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"Could not open the spreadsheet:\n{exc}", parent=self.root)

    # ---------------------------------------------------------- lifecycle
    def _remember(self, output: Path, timeout: int) -> None:
        self.settings.update({
            "input_file": self.input_var.get(),
            "output_dir": str(output.parent),
            "username": self.user_var.get().strip(),
            "timeout": timeout,
            "save_raw": bool(self.raw_var.get()),
            "override": bool(self.override_var.get()),
        })
        save_settings(self.settings)

    def on_close(self) -> None:
        if self.running and not messagebox.askyesno(
                APP_NAME, "An extraction is still running. Quit anyway?", parent=self.root):
            return
        self.stop_event.set()
        self.settings["geometry"] = self.root.geometry()
        save_settings(self.settings)
        self.root.destroy()

    def _callback_error(self, exc_type, exc, tb) -> None:
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        self.log("Unexpected error:\n" + details, "error")
        messagebox.showerror(APP_NAME, f"Unexpected error: {exc}\n\nDetails are in the log.", parent=self.root)


def main() -> None:
    if sys.platform.startswith("win"):
        try:  # crisp text on high-DPI screens
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    StratixExtractorApp(root)
    root.mainloop()
