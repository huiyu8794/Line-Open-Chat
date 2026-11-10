import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from line_openchat_worker import run_channels, secondary_args, region_for
from line_longshot import HalfHourDispatcher
from test_longshot_batches import scene, epoch


class DualChannelTests(unittest.TestCase):
    def args(self, folder):
        return argparse.Namespace(bottom_enabled=True, bottom_y_offset=2510, bottom_height=1255,
            channel_name="上方聊天室", monitor="right", x_offset=0, y_offset=0, width=0, height=1255,
            longshot_dir=folder / "long", interval=10, min_change_pixels=180,
            compare_top=.18, compare_bottom=.14, telegram_backfill=False, run_once=True)

    def test_regions_share_monitor_but_do_not_overlap(self):
        args = self.args(Path("test"))
        display = argparse.Namespace(monitors=[{}, dict(left=3840, top=-1840, width=2160, height=3840)])
        upper = region_for(display, args)
        lower = region_for(display, secondary_args(args, Path("test/captures")))
        self.assertEqual(lower, dict(left=3840, top=670, width=2160, height=1255))
        self.assertLessEqual(upper["top"] + upper["height"], lower["top"])

    def test_identical_scenes_saved_once_per_channel_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            root = folder / "captures" / "upper"
            root.mkdir(parents=True)
            args = self.args(folder)
            frame = scene([("identical in both rooms", "12:01")])
            with patch("line_openchat_worker.get_frame", return_value=(frame, {})), patch("line_openchat_worker.telegram_credentials", return_value=("", "")):
                self.assertEqual(run_channels(args, root), 0)
                self.assertEqual(run_channels(args, root), 0)
            for channel in (root, root.parent / "lower"):
                self.assertEqual(len(list(channel.glob("????-??-??/*.png"))), 1)
                status = json.loads((channel / "worker-status.json").read_text(encoding="utf-8"))
                self.assertEqual(status["skipped_similar"], 1)
                self.assertEqual(status["capture_errors"], 0)

    def test_half_hour_archives_and_captions_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            start = epoch("2026-11-09 12:00:00")
            for name, suffix in (("上方聊天室", "upper"), ("半導體趨勢", "lower")):
                raw, output = base / suffix / "raw", base / suffix / "long"
                (raw / "2026-11-09").mkdir(parents=True)
                scene([("same text", "12:01")]).save(raw / "2026-11-09/12-01-00-000000.png")
                service = HalfHourDispatcher(raw, output, "fake", "fake", active_from=start, channel_name=name)
                service.tick(start + 1799)
                self.assertFalse(list(output.glob("*/*.telegram-pending")))
                service.tick(start + 1802)
                marker = next(output.glob("*/*.telegram-pending"))
                self.assertTrue(json.loads(marker.read_text(encoding="utf-8"))["caption"].startswith(name + "｜"))
                manifest = json.loads((output / "2026-11-09/1200-1230.batch.json").read_text(encoding="utf-8"))
                self.assertEqual(len(manifest["message_ids"]), 1)


if __name__ == "__main__":
    unittest.main()
