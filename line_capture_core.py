"""Shared image comparison and durable Telegram outbox for the tray app."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import ssl
import threading
import time
import traceback
import uuid
from collections import deque
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageChops, ImageFilter

VERSION = "2026.11.10.1"
LOG = logging.getLogger("line_backup")


def telegram_credentials():
    values = []
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        value = os.environ.get(name, "").strip()
        if not value and os.name == "nt":
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(hive, key) as handle:
                        value = str(winreg.QueryValueEx(handle, name)[0]).strip()
                    if value:
                        break
                except OSError:
                    continue
        values.append(value)
    return tuple(values)


def safe_error(exc):
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    token, _ = telegram_credentials()
    return detail.replace(token, "<BOT_TOKEN>") if token else detail


class SceneHistory:
    """Compare chat contents, ignoring chrome, small raster noise and recent repeats."""
    def __init__(self, limit=12, min_pixels=180, top=0.18, bottom=0.14):
        self.frames = deque(maxlen=limit)
        self.min_pixels = min_pixels
        self.top = top
        self.bottom = bottom

    def signature(self, image):
        image = image.convert("RGB")
        # Ratios match the saved LINE window: title/announcement above, input below.
        box = (int(image.width * 0.015), int(image.height * self.top),
               int(image.width * 0.98), int(image.height * (1 - self.bottom)))
        image = image.crop(box)
        width = min(960, image.width)
        height = max(1, round(image.height * width / image.width))
        return image.resize((width, height), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(0.35))

    @staticmethod
    def changed_pixels(left, right):
        if left.size != right.size:
            return 10**9
        diff = ImageChops.difference(left, right)
        red, green, blue = diff.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        return mask.point(lambda value: 255 if value > 28 else 0).histogram()[255]

    def duplicate(self, signature):
        return any(self.changed_pixels(old, signature) < self.min_pixels for old in reversed(self.frames))

    def remember(self, signature):
        self.frames.append(signature)

    def load_recent(self, root):
        # Filenames sort chronologically within ISO-date folders. Decode only recent files.
        candidates = sorted(root.glob("????-??-??/*.png"), reverse=True)
        for path in reversed(candidates[:self.frames.maxlen]):
            try:
                with Image.open(path) as image:
                    self.remember(self.signature(image))
            except (OSError, ValueError):
                LOG.warning("Ignoring unreadable comparison image: %s", path)


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class TelegramFailure(RuntimeError):
    def __init__(self, message, retry_after=0, permanent=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


class TelegramClient:
    def __init__(self, token, chat_id, timeout=25):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        # Keep certificate/hostname verification enabled. The build bundles the
        # ssl runtime's codex-rejected-cas.pem alongside ssl.py.
        self.context = ssl.create_default_context()

    def request(self, method, data=None, content_type=None):
        headers = {"Content-Type": content_type} if content_type else {}
        req = Request(f"https://api.telegram.org/bot{self.token}/{method}", data=data, headers=headers)
        try:
            with urlopen(req, timeout=self.timeout, context=self.context) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                error = {}
            message = error.get("description", str(exc.reason))
            retry = error.get("parameters", {}).get("retry_after", 0)
            raise TelegramFailure(f"Telegram HTTP {exc.code}: {message}", retry,
                                  permanent=exc.code in (400, 401, 403, 413)) from None
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise TelegramFailure(f"Telegram connection error: {exc}") from None
        if not result.get("ok"):
            raise TelegramFailure(result.get("description", "Telegram returned ok=false"))
        return result.get("result")

    def send(self, path, caption=None):
        content = path.read_bytes()
        boundary = "LineBackup" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n'
                f'{self.chat_id}\r\n--{boundary}\r\nContent-Disposition: form-data; '
                f'name="document"; filename="{path.name}"\r\nContent-Type: image/png\r\n\r\n').encode()
        body += content + b"\r\n"
        if caption:
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n').encode("utf-8")
        body += f"--{boundary}--\r\n".encode()
        return self.request("sendDocument", body, f"multipart/form-data; boundary={boundary}")


class TelegramOutbox:
    """Persist receipts before removing jobs; upload on a separate background thread."""
    def __init__(self, root, token, chat_id, compare_scenes=True):
        self.root = root
        self.token = token
        self.chat_id = chat_id
        self.compare_scenes = compare_scenes
        self.client = None
        self.sent_file = root / "telegram-sent.sha256"
        self.sent = set()
        if self.sent_file.exists():
            try:
                self.sent.update(self.sent_file.read_text(encoding="ascii").splitlines())
            except (OSError, UnicodeError):
                LOG.exception("Cannot read sent index; per-file receipts still prevent duplicates")
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.delivered = 0
        self.next_retry = {}
        self.attempts = {}
        self.uncommitted = None
        self.last_error = ""
        self.scenes = SceneHistory()
        for image_path in (sorted(root.glob("????-??-??/*.png"), reverse=True)[:24][::-1] if compare_scenes else []):
            try:
                digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                if digest in self.sent or self.has_receipt(image_path, digest):
                    with Image.open(image_path) as image:
                        self.scenes.remember(self.scenes.signature(image))
            except (OSError, ValueError):
                continue

    @staticmethod
    def has_receipt(path, digest):
        try:
            receipt = json.loads(Path(str(path) + ".telegram-sent.json").read_text(encoding="utf-8"))
            return receipt.get("sha256") == digest
        except (OSError, ValueError):
            return False

    def queue(self, path, caption=None):
        atomic_json(Path(str(path) + ".telegram-pending"), {"version": 1, "queued_at": time.time(), "caption": caption})
        self.wake.set()

    def commit(self, marker, path, digest, result):
        receipt = Path(str(path) + ".telegram-sent.json")
        if not self.has_receipt(path, digest):
            atomic_json(receipt, {"sha256": digest, "message_id": result.get("message_id"),
                                  "sent_at": time.time(), "skipped_similar": bool(result.get("_deduplicated"))})
        self.sent.add(digest)
        # A receipt remains authoritative if the shared text index is unwritable.
        try:
            with self.sent_file.open("a", encoding="ascii") as handle:
                handle.write(digest + "\n")
        except OSError as exc:
            LOG.warning("Receipt saved; shared sent index unavailable: %s", exc)
        marker.unlink(missing_ok=True)

    def drain(self, max_jobs=50):
        if self.uncommitted:
            self.commit(*self.uncommitted)
            self.uncommitted = None
        jobs = sorted(self.root.glob("????-??-??/*.png.telegram-pending"))
        processed = 0
        for marker in jobs:
            if self.stop_event.is_set():
                break
            path = Path(str(marker)[:-len(".telegram-pending")])
            try:
                if not path.is_file():
                    marker.unlink(missing_ok=True)
                    LOG.warning("Removed orphan Telegram marker: %s", path.name)
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if (self.compare_scenes and digest in self.sent) or self.has_receipt(path, digest):
                    marker.unlink(missing_ok=True)
                    continue
                if time.monotonic() < self.next_retry.get(str(path), 0):
                    continue
                if processed >= max_jobs:
                    break
                processed += 1
                with Image.open(path) as image:
                    image.verify()
                signature = None
                if self.compare_scenes:
                    with Image.open(path) as image:
                        signature = self.scenes.signature(image)
                if signature is not None and self.scenes.duplicate(signature):
                    self.commit(marker, path, digest, {"_deduplicated": True})
                    LOG.info("Skipped similar Telegram image: %s", path.name)
                    continue
                if self.client is None:
                    self.client = TelegramClient(self.token, self.chat_id)
                try:
                    job = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    job = {}
                result = self.client.send(path, caption=job["caption"]) if job.get("caption") else self.client.send(path)
                # If the receipt cannot be written, do not send again in this run.
                self.uncommitted = (marker, path, digest, result)
                self.commit(*self.uncommitted)
                self.uncommitted = None
                self.delivered += 1
                if signature is not None:
                    self.scenes.remember(signature)
                self.last_error = ""
                LOG.info("Sent to Telegram: %s (receipt saved)", path.name)
                self.stop_event.wait(1.1)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOG.error("Telegram job failed: %s\n%s", path.name, safe_error(exc))
                if self.uncommitted:
                    LOG.error("Upload succeeded but receipt write failed; pausing sends until state is writable")
                    return False
                attempt = self.attempts.get(str(path), 0) + 1
                self.attempts[str(path)] = attempt
                delay = max(getattr(exc, "retry_after", 0), min(300, 15 * 2**min(attempt - 1, 5)))
                if isinstance(exc, TelegramFailure) and exc.permanent:
                    atomic_json(Path(str(path) + ".telegram-error.json"),
                                {"error": str(exc), "retry_at": time.time() + 300})
                    delay = 300
                self.next_retry[str(path)] = time.monotonic() + delay
                if isinstance(exc, (TelegramFailure, ssl.SSLError, TimeoutError)):
                    # One connection failure applies to the service, not every file.
                    self.stop_event.wait(min(delay, 300))
                    return False
        return True

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.drain()
            except Exception as exc:
                LOG.error("Outbox scan failed; will retry\n%s", safe_error(exc))
                self.stop_event.wait(15)
            self.wake.wait(3)
            self.wake.clear()

    def start(self):
        self.thread = threading.Thread(target=self.run, name="TelegramOutbox", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()


class WorkerLock:
    def __init__(self, root):
        self.path = root / ".capture-worker.lock"
        self.handle = None

    def acquire(self):
        import msvcrt
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        self.handle.seek(1)
        self.handle.write(json.dumps({"pid": os.getpid(), "version": VERSION}).encode())
        self.handle.truncate()
        self.handle.flush()
        return True

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None
