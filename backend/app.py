from __future__ import annotations

import ctypes
import socket
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from backend import BackendServer
from config import AppConfig, append_log, load_config, save_config


KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
ERROR_ALREADY_EXISTS = 183


class MeshToPartApp:
    def __init__(self) -> None:
        self.config = load_config()
        append_log("Application starting")
        self.root: tk.Tk | None = None
        self.single_instance_socket: socket.socket | None = None
        self.single_instance_mutex = None
        if not self._acquire_single_instance_lock():
            append_log(f"Duplicate launch ignored on port {self.config.port}")
            return

        self.root = tk.Tk()
        self.root.title("Mesh To Part Backend")
        self.root.geometry("460x260")
        self.root.minsize(460, 260)
        self.root.resizable(True, True)

        texture_path = self._resource_path("assets", "Studs 4x4 AO Diffuse.png")
        normal_texture_path = self._resource_path("assets", "Studs 4x4 Normal.png")
        self.server = BackendServer(lambda: self.config.api_key, texture_path, normal_texture_path, lambda: self.config.port)
        self.status_var = tk.StringVar(value="Stopped")
        self.help_var = tk.StringVar()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if not self.config.api_key:
            append_log("No API key found, opening setup")
            self.root.after(100, self._open_setup)
        elif self.config.auto_start_server:
            append_log(f"Auto-start requested on port {self.config.port}")
            self.root.after(150, self.start_server)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Mesh To Part Backend", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Run this once, keep it open while Studio uses the plugin, and pin the exe if you want quick access.",
            wraplength=380,
        ).pack(anchor="w", pady=(6, 14))

        status_row = ttk.Frame(frame)
        status_row.pack(fill="x", pady=(0, 10))
        ttk.Label(status_row, text="Status:").pack(side="left")
        ttk.Label(status_row, textvariable=self.status_var).pack(side="left", padx=(6, 0))

        info = ttk.Frame(frame)
        info.pack(fill="x", pady=(0, 14))
        self.backend_var = tk.StringVar()
        ttk.Label(info, textvariable=self.backend_var, wraplength=380).pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(0, 14))
        ttk.Button(buttons, text="Start Server", command=self.start_server).pack(side="left")
        ttk.Button(buttons, text="Stop Server", command=self.stop_server).pack(side="left", padx=8)
        ttk.Button(buttons, text="Settings", command=self._open_setup).pack(side="left")

        help_text = (
            "Plugin values:\n"
            "Backend URL: http://127.0.0.1:{port}\n"
            "Creator Type / ID: match your Open Cloud upload target."
        )
        self.help_var.set(help_text.format(port=self.config.port))
        ttk.Label(frame, textvariable=self.help_var, justify="left").pack(anchor="w")
        self._refresh_info()

    def _refresh_info(self) -> None:
        self.backend_var.set(f"Backend URL: http://127.0.0.1:{self.config.port}")
        self.help_var.set(
            "Plugin values:\n"
            f"Backend URL: http://127.0.0.1:{self.config.port}\n"
            "Creator Type / ID: keep setting those in the plugin."
        )

    def _open_setup(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("First-Time Setup")
        dialog.geometry("460x320")
        dialog.minsize(460, 320)
        dialog.resizable(True, True)
        dialog.transient(self.root)
        dialog.grab_set()

        outer = ttk.Frame(dialog, padding=16)
        outer.pack(fill="both", expand=True)

        frame = ttk.Frame(outer)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Setup", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Add the machine-level backend settings here. Creator type and creator ID stay in the plugin.",
            wraplength=380,
        ).pack(anchor="w", pady=(6, 14))

        api_key_var = tk.StringVar(value=self.config.api_key)
        port_var = tk.StringVar(value=str(self.config.port))

        self._field(frame, "Open Cloud API Key", api_key_var, show="*")
        self._field(frame, "Port", port_var)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", side="bottom", pady=(16, 0))

        def save() -> None:
            api_key = api_key_var.get().strip()
            try:
                port = int(port_var.get().strip())
            except ValueError:
                messagebox.showerror("Invalid Port", "Port must be a number.")
                return

            if not api_key:
                messagebox.showerror("Missing API Key", "An Open Cloud API key is required.")
                return

            self.config = AppConfig(
                api_key=api_key,
                port=port,
                auto_start_server=True,
            )
            save_config(self.config)
            self._refresh_info()
            dialog.destroy()
            self.stop_server()
            self.start_server()

        ttk.Button(actions, text="Save", command=save).pack(side="left")
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="left", padx=8)

    def _field(self, root: ttk.Frame, label: str, variable: tk.StringVar, show: str | None = None) -> None:
        ttk.Label(root, text=label).pack(anchor="w")
        entry = ttk.Entry(root, textvariable=variable, show=show or "")
        entry.pack(fill="x", pady=(2, 10))

    def start_server(self) -> None:
        if not self.config.api_key:
            self._open_setup()
            return
        try:
            self.server.start()
            self.status_var.set(f"Running on http://127.0.0.1:{self.config.port}")
            append_log(f"Server started on port {self.config.port}")
        except Exception as exc:
            append_log(f"Server start failed: {exc!r}")
            self.status_var.set(f"Start failed: {exc}")
            messagebox.showerror("Backend Error", str(exc))

    def stop_server(self) -> None:
        self.server.stop()
        self.status_var.set("Stopped")
        append_log("Server stopped")

    def _on_close(self) -> None:
        self.stop_server()
        if self.single_instance_socket:
            self.single_instance_socket.close()
            self.single_instance_socket = None
        if self.single_instance_mutex:
            KERNEL32.CloseHandle(self.single_instance_mutex)
            self.single_instance_mutex = None
        self.root.destroy()

    def _resource_path(self, *parts: str) -> Path:
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS).joinpath(*parts)
        return Path(__file__).resolve().parent.joinpath(*parts)

    def _acquire_single_instance_lock(self) -> bool:
        mutex_name = "Local\\MeshToPartBackendSingleton"
        ctypes.set_last_error(0)
        mutex = KERNEL32.CreateMutexW(None, False, mutex_name)
        if not mutex:
            raise OSError("Failed to create single-instance mutex.")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            KERNEL32.CloseHandle(mutex)
            return False

        lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lock_socket.bind(("127.0.0.1", 18113))
        except OSError:
            lock_socket.close()
            KERNEL32.CloseHandle(mutex)
            return False
        self.single_instance_mutex = mutex
        self.single_instance_socket = lock_socket
        return True

    def run(self) -> None:
        if self.root is not None:
            self.root.mainloop()


if __name__ == "__main__":
    MeshToPartApp().run()
