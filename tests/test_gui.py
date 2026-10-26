"""Drives the real Tk window with fake switches. Skipped where Tk has no display."""

import tempfile
import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from stratix_extractor import gui
from stratix_extractor.collector import Collector

from .fakes import NetmikoAuthenticationException, make_connect


@pytest.fixture
def app(monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.withdraw()
    # No dialogs or saved settings during tests.
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: False)
    monkeypatch.setattr(gui, "save_settings", lambda s: None)
    connect = make_connect({
        "10.20.3.11": ("stratix5700", "LINE3-SW01#"),
        "10.20.4.52": ("stratix5200", "PKG-SW52>"),
        "10.20.9.9": NetmikoAuthenticationException("Authentication to device failed."),
    })
    factory = lambda log, timeout, raw_dir: Collector(log, timeout=timeout, raw_dir=raw_dir, connect=connect)
    application = gui.StratixExtractorApp(root, collector_factory=factory, settings={})
    yield application
    root.destroy()


def _pump(app, until, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.root.update()
        if until():
            return True
        time.sleep(0.02)
    return False


def test_load_select_reset_and_run(app):
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        switch_list = folder / "switches.csv"
        switch_list.write_text(
            "hostname,ip,username,password\n"
            "LINE3-SW01,10.20.3.11,,\n"
            "PKG-SW52,10.20.4.52,admin,pw\n"
            "BAD-SW,10.20.9.9,,\n",
            encoding="utf-8",
        )
        assert app.load_list(str(switch_list))
        assert len(app.tree.get_children()) == 3
        assert len(app.checked) == 3
        assert app.tree.set("1", "creds") == "From list"

        app.reset_selection()
        assert not app.checked
        assert app.tree.set("0", "sel") == gui.UNTICK

        # Nothing selected -> Go refuses.
        assert app.start_run() is False

        app.select_all()
        app._set_checked("2", False)  # leave the bad switch out at first
        app._set_checked("2", True)
        assert len(app.checked) == 3

        # No default credentials yet -> Go refuses (two switches rely on them).
        assert app.start_run() is False
        app.user_var.set("default")
        app.pass_var.set("pw")

        confirmed = []
        app.confirm = confirmed.append
        app.output_var.set(str(folder / "result.xlsx"))
        assert app.start_run() is True
        assert _pump(app, lambda: confirmed), "run did not finish"

        report = confirmed[0]
        assert [r.success for r in report.results] == [True, True, False]
        assert report.verified and report.output_path == folder / "result.xlsx"
        assert app.tree.set("0", "result").startswith("✔")
        assert app.tree.set("2", "result").startswith("✖")
        assert "Spreadsheet saved and verified" in app.log_text.get("1.0", "end")
        assert str(app.go_btn.cget("state")) == "normal"
