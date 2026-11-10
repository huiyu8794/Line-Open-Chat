"""Regression checks using the user's duplicate samples and isolated outbox jobs."""
import hashlib
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from line_capture_core import SceneHistory, TelegramOutbox, WorkerLock
from line_openchat_worker import save_frame, recover_capture_jobs


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.history = SceneHistory()
        self.image = Image.new("RGB", (2160, 1255), "#2c2d2e")
        ImageDraw.Draw(self.image).rounded_rectangle((90, 500, 950, 610), 25, fill="white")
        self.history.remember(self.history.signature(self.image))

    def test_identical_and_header_or_input_changes_are_ignored(self):
        self.assertTrue(self.history.duplicate(self.history.signature(self.image)))
        copy = self.image.copy()
        draw = ImageDraw.Draw(copy)
        draw.rectangle((0, 0, 2160, 200), fill="blue")
        draw.rectangle((0, 1100, 2160, 1255), fill="red")
        self.assertTrue(self.history.duplicate(self.history.signature(copy)))

    def test_small_reaction_count_change_is_ignored(self):
        copy = self.image.copy()
        ImageDraw.Draw(copy).text((140, 630), "7", fill="white")
        self.assertTrue(self.history.duplicate(self.history.signature(copy)))

    def test_new_single_character_message_is_not_ignored(self):
        copy = self.image.copy()
        draw = ImageDraw.Draw(copy)
        draw.ellipse((16, 740, 76, 800), fill="#819aca")
        draw.rounded_rectangle((90, 750, 160, 801), 15, fill="white")
        draw.text((108, 760), "1", fill="black")
        self.assertFalse(self.history.duplicate(self.history.signature(copy)))

    def test_new_picture_and_recalled_message_are_detected(self):
        copy = self.image.copy()
        ImageDraw.Draw(copy).rectangle((100, 750, 450, 1000), fill="blue")
        self.assertFalse(self.history.duplicate(self.history.signature(copy)))
        copy = self.image.copy()
        ImageDraw.Draw(copy).rectangle((80, 490, 960, 620), fill="#2c2d2e")
        self.assertFalse(self.history.duplicate(self.history.signature(copy)))

    def test_recent_repeated_frame_is_ignored(self):
        copy = self.image.copy()
        ImageDraw.Draw(copy).rectangle((100, 750, 450, 1000), fill="blue")
        self.history.remember(self.history.signature(copy))
        self.assertTrue(self.history.duplicate(self.history.signature(self.image)))

    def test_user_duplicate_samples(self):
        samples = [
            (Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "22-10-53-354.png",
             Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "22-10-53-666.png"),
            (Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "2026-10-23/13-33-19-729.png",
             Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "2026-10-23/13-33-49-696.png"),
            (Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "2026-10-23/13-14-55-870.png",
             Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "2026-10-23/13-15-05-862.png"),
        ]
        checked = 0
        for left, right in samples:
            if not left.exists() or not right.exists():
                continue
            with self.subTest(left=left.name), Image.open(left) as a, Image.open(right) as b:
                signature = self.history.signature(a)
                changed = SceneHistory.changed_pixels(signature, self.history.signature(b))
                self.assertLess(changed, self.history.min_pixels)
                print(f"Sample {left.name}/{right.name}: changed thumbnail pixels={changed}; skipped")
                checked += 1
        if not checked:
            self.skipTest("Private duplicate fixtures unavailable")


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "2026-10-29"
        self.folder.mkdir()
        self.box = TelegramOutbox(self.root, "fake-token", "fake-chat")
        self.box.stop_event.wait = lambda *_: False
        self.box.client = self
        self.calls = 0

    def tearDown(self):
        self.temp.cleanup()

    def send(self, path):
        self.calls += 1
        return {"message_id": 123}

    def image(self, name):
        path = self.folder / name
        Image.new("RGB", (2160, 1255), "white").save(path)
        self.box.queue(path)
        return path

    def test_orphan_does_not_block_valid_job(self):
        self.box.queue(self.folder / "00-missing.png")
        self.image("01-valid.png")
        self.assertTrue(self.box.drain())
        self.assertEqual(self.calls, 1)
        self.assertFalse(list(self.folder.glob("*.telegram-pending")))

    def test_restart_receipt_prevents_resend_if_hash_index_is_missing(self):
        path = self.image("01-valid.png")
        self.box.drain()
        (self.root / "telegram-sent.sha256").unlink()
        other = TelegramOutbox(self.root, "fake-token", "fake-chat")
        other.client = self
        other.stop_event.wait = lambda *_: False
        other.queue(path)
        other.drain()
        self.assertEqual(self.calls, 1)

    def test_similar_upload_is_not_sent_twice(self):
        self.image("01-valid.png")
        self.image("02-copy.png")
        self.box.drain()
        self.assertEqual(self.calls, 1)

    def test_successful_upload_with_receipt_write_failure_is_not_resent(self):
        path = self.image("01-valid.png")
        with patch("line_capture_core.atomic_json", side_effect=PermissionError("test state failure")):
            self.assertFalse(self.box.drain())
        self.assertEqual(self.calls, 1)
        self.box.drain()
        self.assertEqual(self.calls, 1)
        self.assertTrue(Path(str(path) + ".telegram-sent.json").exists())

    def test_corrupt_image_does_not_block_later_job(self):
        corrupt = self.folder / "00-corrupt.png"
        corrupt.write_bytes(b"broken PNG")
        self.box.queue(corrupt)
        self.image("01-valid.png")
        self.box.drain()
        self.assertEqual(self.calls, 1)

    def test_output_file_lock_releases_for_restart(self):
        first, second = WorkerLock(self.root), WorkerLock(self.root)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
        finally:
            first.close()
            second.close()

    def test_cooldown_job_does_not_block_job_beyond_batch_limit(self):
        import time
        bad = self.image("00-cooldown.png")
        self.image("01-valid.png")
        self.box.next_retry[str(bad)] = time.monotonic() + 300
        self.box.drain(max_jobs=1)
        self.assertEqual(self.calls, 1)
        self.assertTrue(Path(str(bad) + ".telegram-pending").exists())

    def test_queue_write_failure_retains_saved_frame_and_recovers_job(self):
        image = Image.new("RGB", (2160, 1255), "white")
        with patch.object(self.box, "queue", side_effect=PermissionError("queue temporarily unavailable")):
            path = save_frame(self.root, image, self.box)
        self.assertTrue(path.exists())
        self.assertTrue(Path(str(path) + ".capture-job").exists())
        recover_capture_jobs(self.root, self.box)
        self.assertFalse(Path(str(path) + ".capture-job").exists())
        self.assertTrue(Path(str(path) + ".telegram-pending").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
