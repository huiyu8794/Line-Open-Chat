"""Capture changed LINE scenes independently from a persistent Telegram uploader."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time
import threading
import copy
from datetime import datetime
from urllib.parse import urlencode

import mss
import mss.tools
from PIL import Image, ImageDraw

from line_capture_core import (VERSION, LOG, SceneHistory, TelegramClient,
                               TelegramOutbox, WorkerLock, atomic_json,
                               safe_error, telegram_credentials)
from line_longshot import HalfHourDispatcher, capture_time, window_start, preview_longshots


def default_output():
    if getattr(sys, "frozen", False):
        folder = Path(sys.executable).resolve().parent
        if folder.name == "_internal":
            folder = folder.parent
        return folder / "captures" / "upper"
    return Path(__file__).resolve().parent / "captures" / "upper"


def configure_logging(root):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    LOG.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    LOG.addHandler(console)
    try:
        file_handler = RotatingFileHandler(root / "worker.log", maxBytes=2_000_000,
                                          backupCount=1, encoding="utf-8")
        file_handler.setFormatter(formatter)
        LOG.addHandler(file_handler)
    except OSError as exc:
        LOG.warning("Cannot open worker.log; keeping console logging: %s", exc)


def region_for(sct, args):
    monitors = sct.monitors[1:]
    if not monitors:
        raise RuntimeError("No display monitors detected; check Windows display/session")
    if args.monitor.casefold() == "right":
        selected = max(monitors, key=lambda item: item["left"] + item["width"])
    else:
        index = int(args.monitor)
        if not 1 <= index <= len(monitors):
            raise ValueError(f"Monitor {index} not found; available monitors: {len(monitors)}")
        selected = monitors[index - 1]
    width = args.width or selected["width"] - args.x_offset
    height = args.height
    if (args.x_offset < 0 or args.y_offset < 0 or width <= 0 or height <= 0
            or args.x_offset + width > selected["width"]
            or args.y_offset + height > selected["height"]):
        raise ValueError(f"Capture rectangle outside monitor {selected['width']}x{selected['height']}")
    return {"left": selected["left"] + args.x_offset,
            "top": selected["top"] + args.y_offset, "width": width, "height": height}


def get_frame(args):
    # Recreate the display handle after each cycle so reconnects/resolution changes recover.
    with mss.MSS() as sct:
        region = region_for(sct, args)
        shot = sct.grab(region)
        image = Image.frombytes("RGB", shot.size, shot.rgb)
    return image, region


def recover_capture_jobs(root, outbox):
    for job in root.glob("????-??-??/*.png.capture-job"):
        path = Path(str(job)[:-len(".capture-job")])
        try:
            data = json.loads(job.read_text(encoding="utf-8"))
            if path.is_file() and data.get("telegram") and outbox:
                if not Path(str(path) + ".telegram-sent.json").exists():
                    outbox.queue(path)
            job.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            LOG.warning("Cannot recover capture job %s: %s", job.name, exc)


def save_frame(root, image, outbox):
    now = datetime.now().astimezone()
    folder = root / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (now.strftime("%H-%M-%S-%f") + ".png")
    job = Path(str(path) + ".capture-job")
    temporary = Path(str(path) + ".tmp")
    atomic_json(job, {"telegram": bool(outbox)})
    try:
        image.save(temporary, format="PNG")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    try:
        if outbox:
            outbox.queue(path)
        job.unlink(missing_ok=True)
    except OSError as exc:
        # The screenshot already exists. Keep its comparison baseline and durable
        # capture-job rather than saving it again every cycle while the outbox fails.
        LOG.error("Saved %s, but queue state needs recovery: %s", path.name, exc)
    return path


def self_check(args, root):
    result = {"version": VERSION, "frozen": bool(getattr(sys, "frozen", False)), "ok": False}
    diagnostics = root / "_diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    result_path = diagnostics / "last-self-check.json"
    try:
        probe = diagnostics / f"write-probe-{os.getpid()}.tmp"
        probe.write_bytes(b"LINE backup write check")
        assert probe.read_bytes() == b"LINE backup write check"
        probe.unlink()
        result["output_writable"] = True
        frame, region = get_frame(args)
        result["capture_size"] = list(frame.size)
        result["capture_region"] = region
        signature = SceneHistory().signature(frame)
        result["identical_frame_is_duplicate"] = SceneHistory.changed_pixels(signature, signature) == 0
        token, chat_id = telegram_credentials()
        if not token or not chat_id:
            raise RuntimeError("Telegram token/chat ID is not configured in process or Windows user environment")
        client = TelegramClient(token, chat_id)
        result["tls_context_created"] = True
        result["tls_verification_enabled"] = bool(client.context.check_hostname)
        client.request("getMe")
        result["telegram_getMe_ok"] = True
        client.request("getChat", urlencode({"chat_id": chat_id}).encode(),
                       "application/x-www-form-urlencoded")
        result["telegram_chat_access_ok"] = True
        if args.test_telegram_upload:
            test_path = diagnostics / ("telegram-connection-test-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".png")
            image = Image.new("RGB", (720, 140), "#1d303b")
            draw = ImageDraw.Draw(image)
            draw.text((20, 25), "LINE screenshot backup - Telegram connection test", fill="white")
            draw.text((20, 60), f"Version {VERSION}  {datetime.now().isoformat(timespec='seconds')}", fill="white")
            image.save(test_path)
            sent = client.send(test_path)
            atomic_json(Path(str(test_path) + ".telegram-sent.json"),
                        {"message_id": sent["message_id"], "sent_at": time.time(), "diagnostic": True})
            result["telegram_test_upload_ok"] = True
            result["receipt_saved"] = True
        result["ok"] = True
    except Exception as exc:
        result["error"] = safe_error(exc)
        LOG.error("Self-check failed\n%s", result["error"])
    atomic_json(result_path, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


def capture_loop(args, root, stop_event=None):
    stop_event = stop_event or threading.Event()
    history = SceneHistory(min_pixels=args.min_change_pixels, top=args.compare_top,
                           bottom=args.compare_bottom)
    history.load_recent(root)
    token, chat_id = telegram_credentials()
    disabled = os.environ.get("LINE_TELEGRAM_DISABLED") == "1"
    longshot_dir = args.longshot_dir
    if longshot_dir is None:
        app = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        if app.name == "_internal":
            app = app.parent
        longshot_dir = Path(os.environ.get("LINE_LONGSHOT_OUTPUT_DIR", str(app / "long_screenshot")))
    send_enabled = bool(token and chat_id and not disabled and os.environ.get("LINE_TELEGRAM_PAUSED") != "1")
    dispatcher = None
    batch_error = ""
    if bool(token) != bool(chat_id) and not disabled:
        LOG.error("Telegram credentials incomplete; screenshots continue locally")
    def start_dispatcher():
        active_from = None
        if args.telegram_backfill:
            sources = sorted(root.glob("????-??-??/*.png"))
            if sources:
                active_from = window_start(capture_time(sources[0]))
        service = HalfHourDispatcher(root, longshot_dir, token, chat_id,
                                     active_from=active_from, send_enabled=send_enabled,
                                     channel_name=getattr(args, "channel_name", ""))
        service.start()
        return service
    try:
        dispatcher = start_dispatcher()
    except Exception as exc:
        batch_error = safe_error(exc)
        LOG.error("Batch initialization failed; local capture continues\n%s", batch_error)
    LOG.info("Worker %s started pid=%s; interval=%s; delivery=half-hour-long-screenshot; Telegram=%s; output=%s; longshots=%s",
             VERSION, os.getpid(), args.interval, send_enabled, root, longshot_dir)
    saved = skipped = failures = 0
    next_heartbeat = time.monotonic()
    next_status = 0.0
    previous_region = None
    try:
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                frame, region = get_frame(args)
                if region != previous_region:
                    LOG.info("Capture region: %s", region)
                    previous_region = region
                signature = history.signature(frame)
                if history.duplicate(signature):
                    skipped += 1
                else:
                    path = save_frame(root, frame, None)
                    history.remember(signature)
                    saved += 1
                    LOG.info("Saved %s", path.name)
                failures = 0
            except Exception as exc:
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    LOG.error("Capture error (will retry; consecutive=%s)\n%s", failures, safe_error(exc))
            if started >= next_heartbeat:
                if dispatcher is None:
                    try:
                        dispatcher = start_dispatcher()
                        batch_error = ""
                    except Exception as exc:
                        batch_error = safe_error(exc)
                        LOG.error("Batch retry failed; local capture continues\n%s", batch_error)
                sender = dispatcher.outbox if dispatcher else None
                LOG.info("Heartbeat saved=%s skipped_similar=%s uploads=%s uploader_error=%s",
                         saved, skipped, sender.delivered if sender else 0,
                         sender.last_error if sender else "disabled")
                next_heartbeat = started + 30
            if started >= next_status:
                try:
                    sender = dispatcher.outbox if dispatcher else None
                    atomic_json(root / "worker-status.json", {
                        "version": VERSION, "pid": os.getpid(), "updated_at": time.time(),
                        "channel_name": getattr(args, "channel_name", ""), "capture_region": previous_region,
                        "saved": saved, "skipped_similar": skipped, "capture_errors": failures,
                        "uploads": sender.delivered if sender else 0,
                        "telegram_enabled": send_enabled,
                        "delivery_mode": "half_hour_long_screenshot", "batch_seconds": 1800,
                        "check_interval": args.interval, "longshot_dir": str(longshot_dir),
                        "batch_error": dispatcher.last_error if dispatcher else batch_error,
                        "uploader_error": sender.last_error if sender else "disabled"})
                except OSError as exc:
                    LOG.warning("Cannot update worker status: %s", exc)
                next_status = started + 5
            if args.run_once:
                return 1 if failures else 0
            delay = max(args.interval, min(30, 2**min(failures, 5))) if failures else args.interval
            stop_event.wait(max(0.05, delay - (time.monotonic() - started)))
    finally:
        if dispatcher:
            dispatcher.stop()


def secondary_args(args, root):
    secondary = copy.copy(args)
    secondary.channel_name = "半導體趨勢"
    secondary.y_offset = args.bottom_y_offset
    secondary.height = args.bottom_height
    secondary.output_dir = root.parent / "lower"
    secondary.longshot_dir = Path(args.longshot_dir or os.environ.get(
        "LINE_LONGSHOT_OUTPUT_DIR", str(root.parent.parent / "long_screenshot"))) / "semiconductor"
    return secondary


def run_channels(args, root):
    if not args.bottom_enabled:
        return capture_loop(args, root)
    secondary = secondary_args(args, root)
    secondary.output_dir.mkdir(parents=True, exist_ok=True)
    lock = WorkerLock(secondary.output_dir)
    if not lock.acquire():
        raise RuntimeError("Secondary channel is already running")
    stop = threading.Event()
    outcome = []
    def run_secondary():
        while not stop.is_set():
            try:
                outcome.append(capture_loop(secondary, secondary.output_dir, stop))
                return
            except Exception as exc:
                LOG.error("Secondary channel failed; retrying: %s", safe_error(exc))
                if args.run_once:
                    outcome.append(1)
                    return
                stop.wait(10)
    thread = threading.Thread(target=run_secondary, name="capture-semiconductor", daemon=True)
    thread.start()
    try:
        result = capture_loop(args, root, stop)
        if args.run_once:
            thread.join()
            return result or (outcome[0] if outcome else 1)
        return result
    finally:
        stop.set()
        thread.join(timeout=20)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="LINE OpenChat screenshot backup worker")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--monitor", default="right")
    parser.add_argument("--x-offset", type=int, default=0)
    parser.add_argument("--y-offset", type=int, default=0)
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=1255)
    parser.add_argument("--bottom-enabled", action="store_true")
    parser.add_argument("--bottom-y-offset", type=int, default=2510)
    parser.add_argument("--bottom-height", type=int, default=1255)
    parser.add_argument("--channel-name", default="上方聊天室")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--longshot-dir", type=Path, default=None)
    parser.add_argument("--preview-longshots", nargs="+", type=Path, default=None)
    parser.add_argument("--compare-top", type=float, default=0.18)
    parser.add_argument("--compare-bottom", type=float, default=0.14)
    parser.add_argument("--min-change-pixels", type=int, default=180)
    parser.add_argument("--telegram-backfill", action="store_true")
    parser.add_argument("--show-telegram-chat-ids", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--test-telegram-upload", action="store_true")
    parser.add_argument("--run-once", action="store_true")
    args = parser.parse_args()
    if args.bottom_y_offset < 0 or args.bottom_height <= 0:
        parser.error("Invalid secondary channel rectangle")
    if args.interval < .5 or args.width < 0 or args.height <= 0 or args.min_change_pixels < 1:
        parser.error("Invalid interval, dimensions or comparison threshold")
    if not 0 <= args.compare_top < .8 or not 0 <= args.compare_bottom < .8 or args.compare_top + args.compare_bottom >= .9:
        parser.error("Invalid comparison margins")
    root = (args.output_dir or Path(os.environ.get("LINE_OPENCHAT_OUTPUT_DIR", str(default_output())))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    configure_logging(root)
    if args.test_telegram_upload and not (args.self_check or args.preview_longshots):
        parser.error("--test-telegram-upload requires --self-check or --preview-longshots")
    if args.preview_longshots:
        destination = args.longshot_dir or root / "_diagnostics" / "longshots"
        report = preview_longshots(args.preview_longshots, destination)
        uploads = 0
        if args.test_telegram_upload:
            token, chat_id = telegram_credentials()
            client = TelegramClient(token, chat_id)
            for page in report["pages"]:
                path = Path(page["path"])
                result = client.send(path, caption="LINE 長截圖驗證：保留已擷取的原訊息，略過收回提示（測試文件）")
                atomic_json(Path(str(path) + ".telegram-sent.json"),
                            {"sha256": page["sha256"], "message_id": result["message_id"], "sent_at": time.time()})
                uploads += 1
        print(json.dumps({"version": VERSION, "preview_only": True, "output": report["output"],
                          "message_count": report["message_count"], "ignored_system_regions": report["ignored_system_regions"],
                          "test_uploads": uploads}, ensure_ascii=False), flush=True)
        return 0
    if args.self_check:
        return self_check(args, root)
    if args.show_telegram_chat_ids:
        token, _ = telegram_credentials()
        result = TelegramClient(token, "").request("getUpdates")
        chats = {}
        for item in result:
            chat = (item.get("message") or item.get("channel_post") or {}).get("chat", {})
            if "id" in chat:
                chats[str(chat["id"])] = chat.get("title") or chat.get("first_name", "chat")
        print(json.dumps(chats, ensure_ascii=False) if chats else "No chat updates. Send /start to your bot, then retry.")
        return 0
    lock = WorkerLock(root)
    if not lock.acquire():
        LOG.warning("Another screenshot worker is already running for %s; duplicate exits with code 10", root)
        return 10
    try:
        return run_channels(args, root)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        LOG.error("Worker stopped after unexpected error\n%s", safe_error(exc))
        return 1
    finally:
        lock.close()
