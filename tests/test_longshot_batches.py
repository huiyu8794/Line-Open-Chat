"""Offline tests: half-hour timing, durable queueing and the supplied recall pair."""
from datetime import datetime
import os
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageFont
from line_longshot import (HalfHourDispatcher, MessageArchive, observations,
                           ordered_matches, window_start, same_message)
from line_capture_core import TelegramOutbox


def epoch(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


def scene(messages, recall=False, photo=False):
    image = Image.new("RGB", (2160, 1255), (45, 46, 48))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 150, 2159, 237), fill=(65, 65, 65))
    draw.rectangle((0, 238, 2159, 1072), fill=(37, 41, 51))
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 20)
    for index, (text, stamp) in enumerate(messages):
        top = 238 + 65 + index * 210
        draw.ellipse((15, top - 34, 72, top + 20), fill="#43acb3")
        draw.text((90, top - 30), "Same sender", font=font, fill="#9ab3c8")
        draw.rounded_rectangle((81, top, 900, top + 100), 15, fill=(246, 246, 246))
        draw.text((103, top + 30), text, font=font, fill="#243441")
        draw.text((912, top + 75), stamp, font=font, fill="#a0a0a0")
    if recall:
        draw.rounded_rectangle((995, 980, 1165, 1020), 17, fill=(56, 61, 70))
        draw.text((1005, 991), "recalled", font=font, fill="#8d98a5")
    if photo:
        draw.rectangle((92, 700, 650, 930), fill="#8b3827")
        draw.ellipse((220, 735, 500, 910), fill="#427fb5")
    return image


class BatchTests(unittest.TestCase):
    def test_clipped_sender_header_does_not_duplicate_full_bubble(self):
        items, _ = observations(scene([("Same message", "12:00")]))
        first = items[0]
        clipped = dict(first)
        header = first["author"]
        clipped["author"] = header.crop((0, 11, header.width, header.height))
        self.assertTrue(same_message(first, clipped))
        different = dict(clipped)
        different["body"] = Image.new("RGB", first["body"].size, "black")
        self.assertFalse(same_message(first, different))

    def test_pink_background_keeps_messages_and_filters_banner(self):
        frame = scene([("First message", "12:01"), ("Second message", "12:02")])
        frame.putdata([(239, 209, 223) if pixel == (37, 41, 51) else pixel for pixel in frame.getdata()])
        ImageDraw.Draw(frame).rounded_rectangle((12, 250, 2146, 370), 8, fill="#aaa077")
        items, details = observations(frame)
        self.assertEqual(details["background"], (239, 209, 223))
        self.assertEqual(len(details["ignored_ad_regions"]), 1)
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["partial_top"])

    def test_pink_lower_batch_renders_at_boundary(self):
        folder = self.root / "captures" / "2026-11-10"
        folder.mkdir(parents=True)
        frame = scene([("Lower channel message", "07:53")])
        frame.putdata([(239, 209, 223) if pixel == (37, 41, 51) else pixel for pixel in frame.getdata()])
        frame.save(folder / "07-53-00-000.png")
        dispatcher = HalfHourDispatcher(folder.parent, self.root / "lower-output",
                                       active_from=epoch("2026-11-10 07:30:00"), send_enabled=False)
        dispatcher.tick(epoch("2026-11-10 07:59:59"))
        self.assertFalse(list(dispatcher.output.glob("*/*.batch.json")))
        dispatcher.tick(epoch("2026-11-10 08:00:03"))
        manifest = json.loads((dispatcher.output / "2026-11-10/0730-0800.batch.json").read_text())
        self.assertEqual(len(manifest["pages"]), 1)
        self.assertEqual(manifest["warnings"], [])

    def test_real_clipped_rims_are_not_messages(self):
        root = Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "2026-11-03"
        for name in ("00-15-49-742002.png", "12-04-50-215773.png", "12-05-50-217055.png"):
            if not (root / name).exists():
                self.skipTest("Private screenshot fixture unavailable")
            with Image.open(root / name) as image:
                items, details = observations(image.convert("RGB"))
            self.assertTrue(details["ignored_edge_remnants"], name)
            self.assertFalse(any(item["partial_top"] and item["body"].height < 12 for item in items), name)

    def test_real_readable_clipped_content_is_retained(self):
        root = Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "2026-11-03"
        for name in ("16-29-50-407744.png", "21-44-20-668353.png"):
            if not (root / name).exists():
                self.skipTest("Private screenshot fixture unavailable")
            with Image.open(root / name) as image:
                items, _ = observations(image.convert("RGB"))
            self.assertTrue(any(item["partial_top"] and item["body"].height >= 12 for item in items), name)

    def test_banner_change_does_not_create_message(self):
        first = scene([("Keep this message", "12:00"), ("Keep photo reply", "12:01")])
        for color in ("#aaa077", "#7788bb"):
            frame = first.copy()
            ImageDraw.Draw(frame).rounded_rectangle((12, 250, 2146, 370), 8, fill=color)
            items, details = observations(frame)
            self.assertEqual(len(details["ignored_ad_regions"]), 1)
            self.assertGreaterEqual(details["viewport"][1], 370)
            if color == "#aaa077":
                earlier = items
            else:
                self.assertEqual(len(ordered_matches(earlier, items)), len(items))

    def test_normal_long_message_not_classified_as_banner(self):
        frame = scene([("Ordinary long message", "12:00")])
        ImageDraw.Draw(frame).rounded_rectangle((81, 303, 1980, 403), 15, fill="#f6f6f6")
        items, details = observations(frame)
        self.assertEqual(details["ignored_ad_regions"], [])
        self.assertEqual(len(items), 1)

    def test_real_ad_frames_keep_one_photo_and_two_texts(self):
        folder = Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "2026-11-04"
        paths = sorted(folder.glob("07-*.png"))
        if len(paths) < 3:
            self.skipTest("Private screenshot fixtures unavailable")
        archive = MessageArchive(self.root / "ads", epoch("2026-11-04 07:00:00"))
        for path in paths:
            report = archive.ingest(path)
            self.assertEqual(len(report["ignored_ad_regions"]), 1)
        # The first old visible suffix is context; the new photo and two replies
        # appear once each, rather than being split by successive ad overlays.
        self.assertEqual(len(archive.state["records"]), 4)

    def test_new_real_outgoing_and_bottom_message_are_retained(self):
        path = Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "2026-10-30/08-25-34-123761.png"
        if not path.exists():
            self.skipTest("Local real-world sample unavailable")
        with Image.open(path) as image:
            items, metadata = observations(image.convert("RGB"))
        self.assertEqual(metadata["viewport"][3], 1073)
        self.assertTrue(any(item["side"] == "outgoing" for item in items))
        self.assertGreaterEqual(items[-1]["bottom"], 780)

    def test_sender_header_is_not_masked_as_reaction(self):
        items, _ = observations(scene([("Hello", "12:00")]))
        self.assertEqual(len(items), 1)
        self.assertIsNotNone(items[0]["author"])
        self.assertGreater(len(set(items[0]["author"].getdata())), 3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw, self.output = self.root / "raw", self.root / "long"
        self.raw.mkdir()
        self.start = epoch("2026-10-29 12:00:00")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, stamp, messages):
        value = datetime.fromtimestamp(stamp)
        folder = self.raw / value.strftime("%Y-%m-%d")
        folder.mkdir(exist_ok=True)
        path = folder / (value.strftime("%H-%M-%S-%f") + ".png")
        scene(messages).save(path)
        return path

    def service(self, start=None):
        return HalfHourDispatcher(self.raw, self.output, "fake-token", "fake-chat",
                                  active_from=self.start if start is None else start)

    def manifest(self, name):
        return json.loads((self.output / "2026-10-29" / (name + ".batch.json")).read_text(encoding="utf-8"))

    def test_no_immediate_upload_then_seal_at_half_hour(self):
        self.write(self.start + 60, [("A", "12:01")])
        service = self.service()
        service.tick(self.start + 1799)
        self.assertFalse(list(self.output.glob("*/*.telegram-pending")))
        self.assertFalse(list(self.raw.glob("*/*.telegram-pending")))
        service.tick(self.start + 1801)
        self.assertFalse(list(self.output.glob("*/*.batch.json")))
        service.tick(self.start + 1802)
        job = self.manifest("1200-1230")
        self.assertEqual(len(job["message_ids"]), 1)
        self.assertEqual(len(list(self.output.glob("*/*.telegram-pending"))), 1)
        marker = next(self.output.glob("*/*.telegram-pending"))
        self.assertIn("12:00–12:30", json.loads(marker.read_text(encoding="utf-8"))["caption"])

    def test_only_new_messages_in_next_window(self):
        self.write(self.start + 60, [("A", "12:01")])
        service = self.service()
        service.tick(self.start + 1802)
        self.write(self.start + 1900, [("A", "12:01"), ("B", "12:31")])
        service.tick(self.start + 3602)
        self.assertEqual(self.manifest("1200-1230")["message_ids"], [1])
        self.assertEqual(self.manifest("1230-1300")["message_ids"], [2])

    def test_baseline_context_is_not_resent(self):
        self.write(self.start - 30, [("A", "11:59")])
        self.write(self.start + 60, [("A", "11:59"), ("B", "12:01")])
        service = self.service()
        service.tick(self.start + 1802)
        self.assertEqual(self.manifest("1200-1230")["message_ids"], [2])

    def test_restart_recovers_ready_batch_without_resending_receipted_page(self):
        self.write(self.start + 60, [("A", "12:01")])
        service = self.service()
        service.tick(self.start + 1802)
        calls = []
        class FakeClient:
            def send(self, path, caption=None):
                calls.append(path)
                return {"message_id": 42}
        service.outbox.client = FakeClient()
        service.outbox.stop_event.wait = lambda *_: False
        service.outbox.drain()
        self.assertEqual(len(calls), 1)
        restarted = self.service()
        restarted.outbox.client = FakeClient()
        restarted.tick(self.start + 1850)
        restarted.outbox.drain()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.manifest("1200-1230")["status"], "sent")

    def test_empty_window_does_not_send_empty_image(self):
        service = self.service()
        service.tick(self.start + 1802)
        self.assertEqual(self.manifest("1200-1230")["status"], "empty")
        self.assertFalse(list(self.output.glob("*/*.png")))

    def test_restart_before_first_frame_preserves_original_active_window(self):
        first = self.service()
        first.tick(self.start + 10)
        second = self.service(self.start + 1800)
        self.assertEqual(second.archive.state["active_from"], self.start)
        self.write(self.start + 60, [("A", "12:01")])
        second.tick(self.start + 1802)
        self.assertEqual(len(self.manifest("1200-1230")["message_ids"]), 1)

    def test_later_full_fragment_is_supplemented_without_overwriting_old_batch(self):
        full = scene([("A long message", "12:01")])
        partial = full.copy()
        partial.paste((37, 41, 51), (0, 238, 2160, 1073))
        partial.paste(full.crop((0, 318, 2160, 1073)), (0, 238))
        folder = self.raw / "2026-10-29"
        folder.mkdir()
        partial.save(folder / "12-01-00-000000.png")
        service = self.service()
        service.tick(self.start + 1802)
        page = Path(self.manifest("1200-1230")["pages"][0]["path"])
        original_digest = hashlib.sha256(page.read_bytes()).hexdigest()
        full.save(folder / "12-31-00-000000.png")
        service.tick(self.start + 3602)
        self.assertEqual(len(service.archive.state["records"]), 1)
        self.assertEqual(self.manifest("1230-1300")["message_ids"], [1])
        self.assertEqual(hashlib.sha256(page.read_bytes()).hexdigest(), original_digest)
        self.assertTrue(self.manifest("1230-1300")["warnings"])

    def test_midnight_window_uses_correct_date_and_boundary(self):
        self.assertEqual(window_start(epoch("2026-10-29 23:59:59")), epoch("2026-10-29 23:30:00"))
        self.assertEqual(window_start(epoch("2026-10-30 00:00:00")), epoch("2026-10-30 00:00:00"))
        self.write(epoch("2026-10-29 23:59:59"), [("A", "23:59")])
        service = self.service(epoch("2026-10-29 23:30:00"))
        service.tick(epoch("2026-10-30 00:00:02"))
        self.assertTrue((self.output / "2026-10-29/2330-0000.batch.json").exists())

    def test_photo_preserved_and_recall_system_pill_excluded(self):
        items, info = observations(scene([("A", "12:01")], recall=True, photo=True))
        self.assertGreaterEqual(len(info["ignored_system_regions"]), 1)
        self.assertTrue(any(item["kind"] == "media" for item in items))

    def test_same_short_text_with_different_timestamps_keeps_two_occurrences(self):
        image = scene([("OK", "12:01"), ("OK", "12:02")])
        path = self.raw / "same.png"
        image.save(path)
        archive = MessageArchive(self.output, self.start)
        archive.ingest(path, self.start + 60)
        self.assertEqual(len(archive.state["records"]), 2)

    def test_corrupt_source_does_not_stop_next_snapshot(self):
        folder = self.raw / "2026-10-29"
        folder.mkdir()
        (folder / "12-00-10-000000.png").write_bytes(b"broken")
        self.write(self.start + 60, [("A", "12:01")])
        service = self.service()
        service.tick(self.start + 1802)
        self.assertEqual(len(self.manifest("1200-1230")["message_ids"]), 1)
        self.assertTrue(self.manifest("1200-1230")["warnings"])

    def test_message_returning_after_it_left_previous_view_is_not_new(self):
        archive = MessageArchive(self.output, self.start)
        first = self.raw / "first.png"
        second = self.raw / "second.png"
        third = self.raw / "third.png"
        scene([("A", "12:01"), ("B", "12:02")]).save(first)
        scene([("B", "12:02")]).save(second)
        scene([("A", "12:01"), ("B", "12:02"), ("C", "12:03")]).save(third)
        archive.ingest(first, self.start + 60)
        archive.ingest(second, self.start + 120)
        result = archive.ingest(third, self.start + 180)
        self.assertEqual(len(archive.state["records"]), 3)
        self.assertEqual(len(result["new_ids"]), 1)

    def test_black_capture_preserves_context_and_does_not_become_message(self):
        self.write(self.start + 60, [("A", "12:01")])
        folder = self.raw / "2026-10-29"
        Image.new("RGB", (2160, 1255), "black").save(folder / "12-02-00-000000.png")
        self.write(self.start + 180, [("A", "12:01"), ("B", "12:03")])
        service = self.service()
        service.tick(self.start + 1802)
        self.assertEqual(len(service.archive.state["records"]), 2)
        self.assertEqual(len(service.state["skipped_sources"]), 1)

    def test_pagination_never_discards_a_message(self):
        path = self.raw / "scene.png"
        scene([("A", "12:01"), ("B", "12:02"), ("C", "12:03")]).save(path)
        archive = MessageArchive(self.output, self.start)
        archive.ingest(path, self.start + 60)
        pages, _ = archive.render(archive.state["order"], self.root / "pages", "test", max_height=300)
        self.assertEqual([i for p in pages for i in p["message_ids"]], archive.state["order"])
        self.assertTrue(all(p["size"][1] <= 300 for p in pages))

    def test_distinct_batch_jobs_are_not_suppressed_by_scene_similarity(self):
        folder = self.output / "2026-10-29"
        folder.mkdir(parents=True)
        first, second = folder / "1200-1230.png", folder / "1230-1300.png"
        scene([("A", "12:01")]).save(first)
        second.write_bytes(first.read_bytes())
        outbox = TelegramOutbox(self.output, "token", "chat", compare_scenes=False)
        outbox.queue(first)
        outbox.queue(second)
        class FakeClient:
            count = 0
            def send(self, path):
                self.count += 1
                return {"message_id": self.count}
        client = FakeClient()
        outbox.client = client
        outbox.stop_event.wait = lambda *_: False
        outbox.drain()
        self.assertEqual(client.count, 2)


class RealRecallCase(unittest.TestCase):
    def test_original_message_is_preserved_after_real_recall(self):
        folder = Path(os.environ.get("LINE_TEST_CAPTURES", "tests/private-captures")) / "2026-10-29"
        before = folder / "20-18-19-478425.png"
        after = folder / "21-00-40-838604.png"
        if not before.exists() or not after.exists():
            self.skipTest("Private recall fixtures unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            archive = MessageArchive(Path(temporary), epoch("2026-10-29 20:00:00"))
            first = archive.ingest(before)
            recalled_id = first["current_ids"][-2]
            saved = archive.load(recalled_id)["block"].tobytes()
            later = archive.ingest(after)
            self.assertGreaterEqual(len(later["ignored_system_regions"]), 1)
            self.assertNotIn(recalled_id, later["current_ids"])
            self.assertEqual(archive.load(recalled_id)["block"].tobytes(), saved)
            self.assertIn(recalled_id, archive.identifiers_for(epoch("2026-10-29 20:00:00")))
            self.assertEqual(len(later["new_ids"]), 1)
            self.assertEqual(len(archive.state["records"]), 11)
            self.assertEqual(archive.identifiers_for(epoch("2026-10-29 21:00:00")), later["new_ids"])
            # Reopening state does not erase the recalled message either.
            reopened = MessageArchive(Path(temporary))
            self.assertEqual(reopened.load(recalled_id)["block"].tobytes(), saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
