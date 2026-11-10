"""Windows system-tray controller for the LINE OpenChat screenshot backup."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import threading
import traceback
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import pystray
from PIL import Image
from line_capture_core import VERSION, telegram_credentials, safe_error, atomic_json


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
CAPTURE_SCRIPT = RESOURCE_DIR / "line_openchat_capture.py"
CAPTURE_EXE = APP_DIR / "_internal" / "line_openchat_capture.exe"
ICON_PNG = RESOURCE_DIR / "line_openchat_robot.png"
SETTINGS_PATH = APP_DIR / "config" / "line_openchat_tray_settings.json"
LOG_PATH = APP_DIR / "logs" / "line_openchat_tray.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
CAPTURE_DIR = APP_DIR / "captures"
MUTEX_NAME = "Local\\LineOpenChatScreenshotBackupTray"

DEFAULTS = {
    "interval": 30.0,
    "monitor": "right",
    "x_offset": 0,
    "y_offset": 0,
    "width": 0,
    "height": 1255,
    "bottom_enabled": True,
    "bottom_y_offset": 2510,
    "bottom_height": 1255,
    "telegram_enabled": True,
    "focus_enabled": False,
    "focus_interval": 60,
    "min_change_pixels": 180,
}

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
LINE_PROCESS_NAMES = {"line.exe", "lineapp.exe", "line.desktop.exe"}
SW_RESTORE = 9
FOREGROUND_IDLE_SECONDS = 60
FOCUS_HOLD_MS = 3500


def acquire_single_instance_mutex():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        ctypes.WinDLL("user32").MessageBoxW(
            None,
            "LINE OpenChat 備份程式已在右下角執行。",
            "LINE OpenChat 備份",
            0x40,
        )
        kernel32.CloseHandle(handle)
        raise SystemExit(0)
    return kernel32, handle


class LineOpenChatTrayApp:
    def __init__(self, kernel32, mutex_handle, diagnostic_only=False):
        self.kernel32 = kernel32
        self.mutex_handle = mutex_handle
        self.diagnostic_only = diagnostic_only
        self.root = tk.Tk()
        threading.excepthook = self.handle_background_thread_error
        self.root.withdraw()
        self.root.title("LINE OpenChat 備份")
        self.settings_window = None
        self.capture_process: subprocess.Popen | None = None
        self.capture_log = None
        self.last_exit_code = None
        self.next_restart_at = None
        self.restart_attempts = 0
        self.worker_started_at = 0
        self.shutting_down = False
        self.lifecycle_first_cycle = "--verify-lifecycle" in sys.argv
        self.config = self.load_config()
        self.next_focus_at = time.monotonic() + float(self.config.get("focus_interval", 60))
        self.icon = pystray.Icon(
            "LineOpenChatBackup",
            Image.open(ICON_PNG).convert("RGBA"),
            "LINE OpenChat 截圖備份",
            menu=self.build_menu(),
        )
        self.icon.run_detached()
        if not self.diagnostic_only:
            self.start_capture()
        self.root.after(1500, self.poll_capture)
        self.root.after(1000, self.focus_tick)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

    @staticmethod
    def default_config():
        config = DEFAULTS.copy()
        config["telegram_enabled"] = bool(
            all(telegram_credentials())
        )
        return config

    def load_config(self):
        config = self.default_config()
        if SETTINGS_PATH.exists():
            try:
                saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    config.update(saved)
            except (OSError, json.JSONDecodeError):
                messagebox.showwarning(
                    "設定檔讀取失敗",
                    f"將使用預設值。設定檔位置：\n{SETTINGS_PATH}",
                )
        return config

    def is_running(self):
        return self.capture_process is not None and self.capture_process.poll() is None

    def build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("設定…", self.menu_open_settings, default=True),
            pystray.MenuItem(
                lambda item: "停止備份" if self.is_running() else "開始備份",
                self.menu_toggle_capture,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("開啟截圖資料夾", self.menu_open_captures),
            pystray.MenuItem("開啟長截圖資料夾", self.menu_open_longshots),
            pystray.MenuItem("開啟執行記錄", self.menu_open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("結束程式", self.menu_exit),
        )

    def update_menu(self):
        if self.icon:
            self.icon.update_menu()

    def notify(self, title, message):
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def capture_command(self):
        if getattr(sys, "frozen", False):
            program = CAPTURE_EXE
            if not program.is_file():
                raise FileNotFoundError(f"找不到截圖執行檔：{program}")
            command = [str(program)]
        else:
            if not CAPTURE_SCRIPT.is_file():
                raise FileNotFoundError(f"找不到截圖腳本：{CAPTURE_SCRIPT}")
            command = [sys.executable, str(CAPTURE_SCRIPT)]

        command.extend(
            [
                "--interval",
                str(self.config["interval"]),
                "--monitor",
                str(self.config["monitor"]),
                "--x-offset",
                str(self.config["x_offset"]),
                "--y-offset",
                str(self.config["y_offset"]),
                "--width",
                str(self.config["width"]),
                "--height",
                str(self.config["height"]),
                "--min-change-pixels",
                str(self.config.get("min_change_pixels", 180)),
            ]
        )
        if self.config.get("bottom_enabled", True):
            command.extend(["--bottom-enabled", "--bottom-y-offset", str(self.config["bottom_y_offset"]),
                            "--bottom-height", str(self.config["bottom_height"])])
        return command

    def start_capture(self):
        if self.is_running():
            return
        self.next_restart_at = None
        token, chat_id = telegram_credentials()
        if self.config.get("telegram_enabled") and not (
            token and chat_id
        ):
            self.notify(
                "Telegram 備份未啟動",
                "請先設定 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID，再重新啟動 app。",
            )
            self.config["telegram_enabled"] = False

        env = os.environ.copy()
        env["LINE_OPENCHAT_OUTPUT_DIR"] = str(CAPTURE_DIR / "upper")
        env["LINE_LONGSHOT_OUTPUT_DIR"] = str(APP_DIR / "long_screenshot")
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # The worker is a separate frozen application, not a child reusing the
        # tray application's PyInstaller extraction directory.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        env["LINE_TELEGRAM_DISABLED"] = "0" if self.config.get("telegram_enabled") else "1"
        # During the optional restart check, queue captures but avoid interrupting
        # an in-flight Telegram request when the first worker is stopped.
        env["LINE_TELEGRAM_PAUSED"] = "1" if self.lifecycle_first_cycle else "0"
        if token and chat_id:
            env["TELEGRAM_BOT_TOKEN"] = token
            env["TELEGRAM_CHAT_ID"] = chat_id
        if not self.config.get("telegram_enabled"):
            env.pop("TELEGRAM_BOT_TOKEN", None)
            env.pop("TELEGRAM_CHAT_ID", None)

        try:
            self.capture_log = LOG_PATH.open("a", encoding="utf-8", buffering=1)
            self.capture_log.write(f"\nTray {VERSION}: starting worker at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            self.capture_process = subprocess.Popen(
                self.capture_command(),
                cwd=APP_DIR,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=self.capture_log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.last_exit_code = None
            self.worker_started_at = time.monotonic()
            self.lifecycle_first_cycle = False
        except Exception as exc:
            if self.capture_log:
                self.capture_log.close()
                self.capture_log = None
            self.capture_process = None
            try:
                with LOG_PATH.open("a", encoding="utf-8") as error_log:
                    error_log.write(safe_error(exc) + "\n")
            except OSError:
                pass
            self.next_restart_at = time.monotonic() + 15
            self.notify("暫時無法啟動備份", "15 秒後重試；詳細原因已寫入執行記錄。")
        self.update_menu()

    def stop_capture(self, quiet=False):
        self.next_restart_at = None
        process = self.capture_process
        if process is None:
            return
        if process.poll() is None:
            try:
                # Onefile workers have a bootloader parent and a Python child.
                # Terminating only the parent leaves an orphan holding the lock.
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                process.kill()
                process.wait(timeout=3)
        self.last_exit_code = process.returncode
        self.capture_process = None
        if self.capture_log:
            self.capture_log.close()
            self.capture_log = None
        self.update_menu()
        if not quiet:
            self.notify("LINE OpenChat 備份", "截圖監測已停止。")

    def restart_capture(self):
        self.stop_capture(quiet=True)
        self.start_capture()

    def poll_capture(self):
        process = self.capture_process
        if process is not None and process.poll() is not None:
            self.last_exit_code = process.returncode
            self.capture_process = None
            if self.capture_log:
                self.capture_log.close()
                self.capture_log = None
            self.update_menu()
            if self.last_exit_code == 10:
                self.notify("已有備份程序執行中", "偵測到同一截圖目錄已有 worker，請避免重複啟動。")
            elif not self.shutting_down:
                self.restart_attempts += 1
                delay = min(300, 5 * 2 ** min(self.restart_attempts - 1, 6))
                self.next_restart_at = time.monotonic() + delay
                self.notify("備份程序已停止，準備重試", f"結束碼 {self.last_exit_code}；{delay} 秒後重新啟動，詳見執行記錄。")
        elif process is not None and time.monotonic() - self.worker_started_at > 60:
            self.restart_attempts = 0
        if not self.shutting_down and self.next_restart_at and time.monotonic() >= self.next_restart_at:
            self.start_capture()
        self.root.after(1500, self.poll_capture)

    def toggle_capture(self):
        if self.is_running():
            self.stop_capture()
        else:
            self.start_capture()

    def verify_lifecycle(self):
        """Exercise the actual tray stop/start path once and leave backup running."""
        old_pid = self.capture_process.pid if self.capture_process else None
        self.restart_capture()
        def report():
            try:
                status = json.loads((CAPTURE_DIR / "upper" / "worker-status.json").read_text(encoding="utf-8"))
                fresh = time.time() - status.get("updated_at", 0) < max(45, self.config["interval"] * 2)
                progressed = status.get("saved", 0) + status.get("skipped_similar", 0) > 0
                result = {"version": VERSION, "tray_icon_visible": bool(self.icon.visible),
                          "worker_running": self.is_running(), "worker_status_fresh": fresh,
                          "capture_progressed": progressed, "capture_errors": status.get("capture_errors"),
                          "old_bootloader_pid": old_pid,
                          "new_bootloader_pid": self.capture_process.pid if self.capture_process else None,
                          "worker_version": status.get("version")}
                result["ok"] = bool(result["tray_icon_visible"] and result["worker_running"] and fresh
                                    and progressed and result["capture_errors"] == 0
                                    and result["old_bootloader_pid"] != result["new_bootloader_pid"]
                                    and result["worker_version"] == VERSION)
                if self.config.get("bottom_enabled", True):
                    secondary = json.loads((CAPTURE_DIR / "lower" / "worker-status.json").read_text(encoding="utf-8"))
                    result["secondary_ok"] = bool(secondary.get("version") == VERSION
                        and secondary.get("pid") == status.get("pid")
                        and time.time() - secondary.get("updated_at", 0) < max(45, self.config["interval"] * 2)
                        and secondary.get("capture_errors") == 0
                        and secondary.get("saved", 0) + secondary.get("skipped_similar", 0) > 0)
                    result["ok"] = result["ok"] and result["secondary_ok"]
            except Exception as exc:
                result = {"version": VERSION, "ok": False, "error": safe_error(exc)}
            (APP_DIR / "logs" / "runtime-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write("Tray lifecycle check: " + json.dumps(result, ensure_ascii=False) + "\n")
        self.root.after(14000, report)

    def menu_toggle_capture(self, icon, item):
        self.root.after(0, self.toggle_capture)

    def menu_open_settings(self, icon, item):
        self.root.after(0, self.open_settings)

    def menu_open_captures(self, icon, item):
        self.root.after(0, lambda: self.open_folder(CAPTURE_DIR))

    def menu_open_longshots(self, icon, item):
        self.root.after(0, lambda: self.open_folder(APP_DIR / "long_screenshot"))

    def menu_open_log(self, icon, item):
        self.root.after(0, self.open_log)

    def menu_exit(self, icon, item):
        self.root.after(0, self.shutdown)

    @staticmethod
    def open_folder(path):
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_log(self):
        if not LOG_PATH.exists():
            LOG_PATH.touch()
        os.startfile(LOG_PATH)

    def handle_background_thread_error(self, args):
        details = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        try:
            with LOG_PATH.open("a", encoding="utf-8") as log_file:
                log_file.write("\n--- Background thread error ---\n")
                log_file.write(details.rstrip() + "\n")
        except Exception:
            pass
        summary = str(args.exc_value) or args.exc_type.__name__
        try:
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "LINE OpenChat 備份系統匣啟動失敗",
                    f"無法建立右下角圖示：{summary}\n\n詳細記錄：\n{LOG_PATH}",
                    parent=self.root,
                ),
            )
        except Exception:
            pass

    @staticmethod
    def _configure_user32(user32):
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)
        ]
        user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
        user32.IsWindowVisible.restype = ctypes.wintypes.BOOL
        user32.GetWindowRect.argtypes = [
            ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.RECT)
        ]
        user32.GetWindowRect.restype = ctypes.wintypes.BOOL
        user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.wintypes.LPARAM]
        user32.EnumWindows.restype = ctypes.wintypes.BOOL
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
        user32.IsIconic.restype = ctypes.wintypes.BOOL
        user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
        user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
        user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
        user32.IsWindow.restype = ctypes.wintypes.BOOL
        return user32

    @staticmethod
    def _window_process_name(hwnd):
        user32 = LineOpenChatTrayApp._configure_user32(
            ctypes.WinDLL("user32", use_last_error=True)
        )
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD
        ]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        process = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not process:
            return ""
        try:
            length = ctypes.wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(length.value)
            query = kernel32.QueryFullProcessImageNameW
            query.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.wintypes.DWORD,
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.wintypes.DWORD),
            ]
            query.restype = ctypes.wintypes.BOOL
            if not query(process, 0, buffer, ctypes.byref(length)):
                return ""
            return Path(buffer.value).name.casefold()
        finally:
            kernel32.CloseHandle(process)

    @classmethod
    def _find_line_window(cls):
        user32 = cls._configure_user32(
            ctypes.WinDLL("user32", use_last_error=True)
        )
        matches = []
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )

        def collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            process_name = cls._window_process_name(hwnd)
            if process_name in LINE_PROCESS_NAMES:
                rect = ctypes.wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    area = max(0, rect.right - rect.left) * max(
                        0, rect.bottom - rect.top
                    )
                    matches.append((area, hwnd))
            return True

        callback = callback_type(collect)
        user32.EnumWindows.argtypes = [callback_type, ctypes.wintypes.LPARAM]
        user32.EnumWindows(callback, 0)
        return max(matches, default=(0, None), key=lambda entry: entry[0])[1]

    @staticmethod
    def _last_input_tick():
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.UINT),
                ("dwTime", ctypes.wintypes.DWORD),
            ]

        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
        user32.GetLastInputInfo.restype = ctypes.wintypes.BOOL
        if user32.GetLastInputInfo(ctypes.byref(info)):
            return info.dwTime
        return None

    def focus_tick(self):
        if self.config.get("focus_enabled") and time.monotonic() >= self.next_focus_at:
            self.next_focus_at = time.monotonic() + float(
                self.config.get("focus_interval", 60)
            )
            idle_tick = self._last_input_tick()
            if idle_tick is not None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.GetTickCount.restype = ctypes.wintypes.DWORD
                elapsed_ms = (kernel32.GetTickCount() - idle_tick) & 0xFFFFFFFF
                if elapsed_ms >= FOREGROUND_IDLE_SECONDS * 1000:
                    self.focus_line_briefly(idle_tick)
        self.root.after(1000, self.focus_tick)

    def focus_line_briefly(self, last_input_tick):
        user32 = self._configure_user32(
            ctypes.WinDLL("user32", use_last_error=True)
        )
        line_hwnd = self._find_line_window()
        if not line_hwnd:
            return
        foreground = user32.GetForegroundWindow()
        if not foreground or self._window_process_name(foreground) in LINE_PROCESS_NAMES:
            return
        if user32.IsIconic(line_hwnd):
            user32.ShowWindow(line_hwnd, SW_RESTORE)
        if not user32.SetForegroundWindow(line_hwnd):
            return

        def restore_previous_window():
            # Respect any user interaction that happened while LINE was foreground.
            if (
                user32.GetForegroundWindow() == line_hwnd
                and self._last_input_tick() == last_input_tick
                and user32.IsWindow(foreground)
            ):
                user32.SetForegroundWindow(foreground)

        self.root.after(FOCUS_HOLD_MS, restore_previous_window)

    def open_settings(self):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("LINE OpenChat 備份設定")
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        container = ttk.Frame(window, padding=14)
        container.grid(sticky="nsew")

        fields = [
            ("檢查間隔（秒）", "interval"),
            ("螢幕（right 或編號）", "monitor"),
            ("左側偏移 X", "x_offset"),
            ("上方偏移 Y", "y_offset"),
            ("截圖寬度（0=剩餘寬度）", "width"),
            ("截圖高度", "height"),
            ("下方聊天室上方偏移 Y", "bottom_y_offset"),
            ("下方聊天室截圖高度", "bottom_height"),
            ("切換焦點間隔（秒）", "focus_interval"),
            ("變動門檻（越大越不易截圖）", "min_change_pixels"),
        ]
        variables = {}
        for row, (label, key) in enumerate(fields):
            ttk.Label(container, text=label).grid(row=row, column=0, sticky="w", pady=4)
            variable = tk.StringVar(value=str(self.config.get(key, DEFAULTS[key])))
            variables[key] = variable
            ttk.Entry(container, textvariable=variable, width=24).grid(
                row=row, column=1, sticky="ew", padx=(12, 0), pady=4
            )

        telegram_var = tk.BooleanVar(value=bool(self.config.get("telegram_enabled", False)))
        ttk.Checkbutton(
            container,
            text="每半小時備份長截圖至 Telegram",
            variable=telegram_var,
        ).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(10, 8))
        focus_var = tk.BooleanVar(value=bool(self.config.get("focus_enabled", False)))
        ttk.Checkbutton(
            container,
            text="定時短暫切到 LINE 前景，再切回原視窗",
            variable=focus_var,
        ).grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(
            container,
            text="焦點切換只會在電腦閒置至少 1 分鐘時執行，LINE 約顯示 3.5 秒後切回原視窗，以便截圖程式取得更新畫面；可能有短暫畫面跳動。監測右上角固定範圍，畫面被遮住或電腦鎖定時，截圖可能不正確。",
            wraplength=370,
        ).grid(row=len(fields) + 2, column=0, columnspan=2, sticky="w", pady=(0, 10))

        buttons = ttk.Frame(container)
        bottom_var = tk.BooleanVar(value=bool(self.config.get("bottom_enabled", True)))
        ttk.Checkbutton(container, text="同時備份下方「半導體趨勢」（各自合併與傳送）",
                        variable=bottom_var).grid(row=len(fields) + 3, column=0, columnspan=2, sticky="w", pady=8)
        buttons.grid(row=len(fields) + 4, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="取消", command=window.destroy).pack(side="right")

        def save_settings():
            try:
                interval = float(variables["interval"].get())
                focus_interval = int(variables["focus_interval"].get())
                if interval < 0.5:
                    raise ValueError("檢查間隔至少要 0.5 秒。")
                if focus_interval < 15:
                    raise ValueError("切換焦點間隔至少要 15 秒。")
                monitor = variables["monitor"].get().strip()
                if monitor.casefold() != "right":
                    monitor_index = int(monitor)
                    if monitor_index < 1:
                        raise ValueError("螢幕編號需為 1 或更大的整數，或填 right。")
                values = {
                    "interval": interval,
                    "monitor": monitor,
                    "x_offset": int(variables["x_offset"].get()),
                    "y_offset": int(variables["y_offset"].get()),
                    "width": int(variables["width"].get()),
                    "height": int(variables["height"].get()),
                    "bottom_enabled": bool(bottom_var.get()),
                    "bottom_y_offset": int(variables["bottom_y_offset"].get()),
                    "bottom_height": int(variables["bottom_height"].get()),
                    "telegram_enabled": bool(telegram_var.get()),
                    "focus_enabled": bool(focus_var.get()),
                    "focus_interval": focus_interval,
                    "min_change_pixels": int(variables["min_change_pixels"].get()),
                }
                if values["x_offset"] < 0 or values["y_offset"] < 0 or values["bottom_y_offset"] < 0:
                    raise ValueError("X/Y 偏移不能小於 0。")
                if values["width"] < 0 or values["height"] <= 0 or values["bottom_height"] <= 0:
                    raise ValueError("寬度不可小於 0，高度需大於 0。")
                if values["min_change_pixels"] < 1:
                    raise ValueError("變動門檻需為正整數，建議先使用 180。")
                if values["telegram_enabled"] and not (
                    all(telegram_credentials())
                ):
                    raise ValueError("請先設定 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。")
            except ValueError as exc:
                messagebox.showerror("設定值無效", str(exc), parent=window)
                return

            try:
                atomic_json(SETTINGS_PATH, values)
            except OSError as exc:
                messagebox.showerror("無法儲存設定", str(exc), parent=window)
                return
            self.config = values
            self.next_focus_at = time.monotonic() + values["focus_interval"]
            window.destroy()
            self.settings_window = None
            self.restart_capture()
            self.notify("LINE OpenChat 備份", "設定已儲存，監測程式已重新啟動。")

        ttk.Button(buttons, text="儲存並套用", command=save_settings).pack(
            side="right", padx=(0, 8)
        )
        window.bind("<Return>", lambda event: save_settings())
        window.bind("<Escape>", lambda event: window.destroy())
        window.update_idletasks()
        x = max(0, (window.winfo_screenwidth() - window.winfo_width()) // 2)
        y = max(0, (window.winfo_screenheight() - window.winfo_height()) // 2)
        window.geometry(f"+{x}+{y}")
        window.grab_set()
        window.focus_force()

    def shutdown(self):
        self.shutting_down = True
        self.stop_capture(quiet=True)
        try:
            self.icon.stop()
        except Exception:
            pass
        self.kernel32.ReleaseMutex(self.mutex_handle)
        self.kernel32.CloseHandle(self.mutex_handle)
        self.root.quit()

    def run(self):
        self.root.mainloop()


def main():
    if "--version" in sys.argv:
        print(VERSION)
        return
    if os.name != "nt":
        raise SystemExit("This tray app currently supports Windows only.")
    diagnostic_only = "--diagnose" in sys.argv[1:]
    try:
        if not ICON_PNG.is_file():
            raise FileNotFoundError(f"找不到系統匣圖示：{ICON_PNG}")
        kernel32, mutex_handle = acquire_single_instance_mutex()
        app = LineOpenChatTrayApp(kernel32, mutex_handle, diagnostic_only)
        if "--verify-lifecycle" in sys.argv:
            app.root.after(10000, app.verify_lifecycle)
        app.run()
    except SystemExit as exc:
        if exc.code in (None, 0):
            return
        error = str(exc)
        exit_code = exc.code if isinstance(exc.code, int) else 1
        show_startup_error(error, exit_code)
    except Exception:
        show_startup_error(traceback.format_exc(), 1)


def show_startup_error(details, exit_code):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write("\n--- App startup error ---\n")
            log_file.write(details.rstrip() + "\n")
    except Exception:
        pass
    short_details = details.strip().splitlines()[-1] if details.strip() else "未知錯誤"
    try:
        ctypes.WinDLL("user32", use_last_error=True).MessageBoxW(
            None,
            f"LINE OpenChat 備份程式無法啟動。\n\n{short_details}\n\n詳細記錄：\n{LOG_PATH}",
            "LINE OpenChat 備份啟動失敗",
            0x10,
        )
    except Exception:
        pass
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
