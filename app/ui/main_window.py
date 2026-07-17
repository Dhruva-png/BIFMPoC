"""
Minimal Tkinter desktop UI. Chosen over PyQt/Kivy deliberately: it ships with
the Python standard library, so the POC has zero extra GUI dependencies to
install on a locked-down Windows machine.
"""
from __future__ import annotations

import os
import platform
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from app.core.pipeline import run_batch
from app.llm.router import check_connection, active_provider
from app.utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)


class BifmApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("BIFM Unit Trusts - Document Processing (Local POC)")
        self.geometry("760x560")
        self.minsize(640, 480)

        self.intake_var = tk.StringVar(value=str(settings.paths.intake_dir))
        self.channel_var = tk.StringVar(value="Unknown")
        self.status_var = tk.StringVar(value="Idle")
        self.last_report_path: Path | None = None

        self._build_layout()
        self._check_llm_async()

    # ------------------------------------------------------------------ UI
    def _build_layout(self) -> None:
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Intake folder:").pack(side="left")
        ttk.Entry(top, textvariable=self.intake_var, width=55).pack(side="left", padx=6)
        ttk.Button(top, text="Browse...", command=self._browse_intake).pack(side="left")

        channel_row = ttk.Frame(self)
        channel_row.pack(fill="x", **pad)
        ttk.Label(channel_row, text="Submission channel:").pack(side="left")
        ttk.Combobox(
            channel_row, textvariable=self.channel_var, state="readonly",
            values=["Email", "Walk-in", "Unknown"], width=12,
        ).pack(side="left", padx=6)

        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)

        self.run_button = ttk.Button(actions, text="Run Batch", command=self._on_run_clicked)
        self.run_button.pack(side="left")

        self.open_report_button = ttk.Button(actions, text="Open Last Report", command=self._open_report, state="disabled")
        self.open_report_button.pack(side="left", padx=8)

        self.llm_status_label = ttk.Label(actions, text=f"Checking {active_provider()} connection...", foreground="gray")
        self.llm_status_label.pack(side="right")

        ttk.Label(self, textvariable=self.status_var, foreground="blue").pack(fill="x", padx=10)

        log_frame = ttk.LabelFrame(self, text="Processing Log")
        log_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_box = scrolledtext.ScrolledText(log_frame, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True)

    # --------------------------------------------------------------- logic
    def _browse_intake(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.intake_var.get())
        if chosen:
            self.intake_var.set(chosen)

    def _check_llm_async(self) -> None:
        def worker():
            ok = check_connection()
            provider = active_provider()
            text = f"{provider}: connected" if ok else f"{provider}: NOT reachable - see README setup"
            color = "green" if ok else "red"
            self.llm_status_label.config(text=text, foreground=color)
        threading.Thread(target=worker, daemon=True).start()

    def _append_log(self, message: str) -> None:
        def do_append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, do_append)

    def _on_run_clicked(self) -> None:
        intake_dir = Path(self.intake_var.get())
        if not intake_dir.exists():
            messagebox.showerror("Folder not found", f"{intake_dir} does not exist.")
            return

        self.run_button.config(state="disabled")
        self.status_var.set("Running batch...")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        thread = threading.Thread(target=self._run_batch_worker, args=(intake_dir, self.channel_var.get()), daemon=True)
        thread.start()

    def _run_batch_worker(self, intake_dir: Path, channel: str) -> None:
        try:
            outcomes, report_path = run_batch(intake_dir, progress_cb=self._append_log, channel=channel)
            self.last_report_path = report_path
            summary = (
                f"Done. {len(outcomes)} document(s) processed. "
                f"Statuses: " + ", ".join(f"{o.filename}={o.validation_status}" for o in outcomes)
                if outcomes else "No documents found in intake folder."
            )
            self.after(0, lambda: self.status_var.set(summary))
            if report_path.exists():
                self.after(0, lambda: self.open_report_button.config(state="normal"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Batch run failed")
            # Python unbinds `exc` as soon as this except block exits, but
            # self.after() runs its lambdas later on Tkinter's event loop -
            # by then `exc` no longer exists, so capture the message now.
            error_msg = str(exc)
            self.after(0, lambda: self.status_var.set(f"Batch failed: {error_msg}"))
            self.after(0, lambda: messagebox.showerror("Batch failed", error_msg))
        finally:
            self.after(0, lambda: self.run_button.config(state="normal"))

    def _open_report(self) -> None:
        if not self.last_report_path or not self.last_report_path.exists():
            messagebox.showwarning("No report", "No report has been generated yet.")
            return
        path_str = str(self.last_report_path)
        if platform.system() == "Windows":
            os.startfile(path_str)  # noqa: S606 - intentional, user-initiated, local file
        elif platform.system() == "Darwin":
            subprocess.run(["open", path_str], check=False)
        else:
            subprocess.run(["xdg-open", path_str], check=False)


def launch() -> None:
    app = BifmApp()
    app.mainloop()
