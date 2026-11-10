"""Persistent message archive and wall-clock half-hour delivery."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from itertools import groupby
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from PIL import Image, ImageChops, ImageDraw, ImageFont
from line_capture_core import LOG, atomic_json, safe_error, TelegramOutbox


class NonChatFrame(ValueError):
    pass


def window_start(timestamp):
    value = datetime.fromtimestamp(timestamp)
    return value.replace(minute=value.minute // 30 * 30, second=0, microsecond=0).timestamp()


def capture_time(path):
    return datetime.strptime(path.parent.name + " " + path.stem, "%Y-%m-%d %H-%M-%S-%f").timestamp()


def max_band(image):
    a, b, c = image.split()
    return ImageChops.lighter(ImageChops.lighter(a, b), c)


def changed_pixels(left, right):
    if left.size != right.size:
        if abs(left.width - right.width) > 3 or abs(left.height - right.height) > 3:
            return 10**9
        box = (0, 0, min(left.width, right.width), min(left.height, right.height))
        left, right = left.crop(box), right.crop(box)
    return max_band(ImageChops.difference(left, right)).point(lambda p: 255 if p > 26 else 0).histogram()[255]


def trim_ink(image, background):
    mask = max_band(ImageChops.difference(image, Image.new("RGB", image.size, background))).point(lambda p: 255 if p > 18 else 0)
    bounds = mask.getbbox()
    return image.crop(bounds) if bounds else None


def viewport(image):
    pixels = image.load()
    runs = []
    # A right-aligned outgoing bubble can cover a right-hand probe. The left
    # gutter stays outside avatars/bubbles; use several independent probes.
    for x in (8, 12, max(0, image.width - 36), max(0, image.width - 8)):
        start = 0
        for color, rows in groupby(pixels[x, y] for y in range(image.height)):
            length = sum(1 for _ in rows)
            # Geometry identifies the viewport, not dark-theme brightness.
            # Custom solid backgrounds (e.g. pink) are valid too.
            if start > .1 * image.height and length > .3 * image.height and 12 <= max(color) and min(color) < 245:
                runs.append((length, start, start + length, color))
            start += length
    if not runs:
        raise ValueError("Cannot identify LINE chat viewport; raw screenshots retained")
    _, top, bottom, background = max(runs)
    return image.crop((0, top, image.width - 18, bottom)), background, (0, top, image.width - 18, bottom)


def exclude_top_banner(chat, background):
    """LINE overlays span both gutters; message bubbles stay inside them.

    Treat the area below a detected overlay as a new viewport edge, so visible
    suffixes match earlier full messages rather than becoming ad/message hybrids.
    No ad creative, text, or fixed desktop coordinates are required.
    """
    mask = max_band(ImageChops.difference(chat, Image.new("RGB", chat.size, background))).point(lambda p: 255 if p > 28 else 0)
    data, width = mask.tobytes(), chat.width
    spans, start = [], None
    for y in range(min(chat.height, round(chat.height * .35)) + 1):
        row = data[y * width:(y + 1) * width]
        active = (len(row) == width and row.count(255) > width * .94
                  and row[round(width * .012)] and row[round(width * .988)])
        if active and start is None:
            start = y
        elif not active and start is not None:
            if start < chat.height * .12 and width * .018 <= y - start <= width * .09:
                spans.append((start, y))
            start = None
    if not spans:
        return chat, 0, []
    top, bottom = spans[0]
    # Rounded corners may make the full-width scan start/end a few rows inside
    # the actual rectangle. Follow the banner's outer gutter pixels to its end.
    probe = round(width * .02)
    while bottom < chat.height and data[bottom * width + probe] == 255:
        bottom += 1
    return chat.crop((0, bottom, width, chat.height)), bottom, [(0, top, width, bottom)]


def observations(image):
    if max(image.convert("L").getextrema()) <= 8:
        raise NonChatFrame("black screenshot; no visible chat pixels")
    chat, background, crop_box = viewport(image)
    chat, removed_top, ad_regions = exclude_top_banner(chat, background)
    crop_box = (crop_box[0], crop_box[1] + removed_top, crop_box[2], crop_box[3])
    difference = max_band(ImageChops.difference(chat, Image.new("RGB", chat.size, background)))
    foreground = difference.point(lambda p: 255 if p > 24 else 0)
    left = max(1, round(chat.width * .0366))
    body_mask = foreground.crop((left, 0, chat.width, chat.height))
    data, width = body_mask.tobytes(), body_mask.width
    occupied = [data[y * width:(y + 1) * width].count(255) >= 4 for y in range(chat.height)]
    # Join thin separators/antialias gaps, but not separate consecutive bubbles.
    for y in range(1, len(occupied) - 2):
        if occupied[y - 1] and not occupied[y] and (occupied[y + 1] or occupied[y + 2]):
            occupied[y] = True
    spans, start = [], None
    for y, active in enumerate(occupied + [False]):
        if active and start is None:
            start = y
        elif not active and start is not None:
            spans.append((start, y))
            start = None
    white = ImageChops.darker(ImageChops.darker(*chat.split()[:2]), chat.split()[2]).point(lambda p: 255 if p >= 225 else 0)
    white = ImageChops.multiply(white, foreground)
    red, green, blue = chat.split()
    green_mask = ImageChops.multiply(
        ImageChops.subtract(green, red).point(lambda p: 255 if p > 20 else 0),
        ImageChops.subtract(green, blue).point(lambda p: 255 if p > 20 else 0))
    green_mask = ImageChops.multiply(green_mask, green.point(lambda p: 255 if p > 140 else 0))
    items, systems, reactions, read_receipts = [], [], [], []
    edge_remnants = []
    # Recall/date pills have a low-contrast fill. Detect them separately at a
    # lower threshold so their text cannot leak into a neighboring sender header.
    center_left, center_right = round(chat.width * .28), round(chat.width * .72)
    center = difference.point(lambda p: 255 if p > 10 else 0).crop((center_left, 0, center_right, chat.height))
    cd = center.tobytes()
    runs, run_start = [], None
    for y in range(chat.height + 1):
        active = y < chat.height and cd[y * center.width:(y + 1) * center.width].count(255) >= 20
        if active and run_start is None:
            run_start = y
        elif not active and run_start is not None:
            runs.append((run_start, y))
            run_start = None
    for top, bottom in runs:
        bounds = center.crop((0, top, center.width, bottom)).getbbox()
        if not bounds or not 18 <= bottom - top <= 80 or bounds[0] < 5 or bounds[2] > center.width - 5:
            continue
        x0, x1 = center_left + bounds[0], center_left + bounds[2]
        whites = white.crop((x0, top, x1, bottom)).histogram()[255]
        if whites < .08 * (x1 - x0) * (bottom - top):
            systems.append((x0 - 3, max(0, top - 3), x1 + 3, min(chat.height, bottom + 3)))
    for top, bottom in spans:
        bounds = body_mask.crop((0, top, body_mask.width, bottom)).getbbox()
        x0, x1 = bounds[0] + left, bounds[2] + left
        height = bottom - top
        region = foreground.crop((x0, top, x1, bottom))
        area = region.histogram()[255]
        whites = white.crop((x0, top, x1, bottom)).histogram()[255]
        # Do not promote a clipped rounded bubble rim to a new message.
        if (top == 0 or bottom == chat.height) and height < max(4, round(chat.width * .0056)) and whites / max(area, 1) > .65:
            edge_remnants.append((x0, top, x1, bottom))
            continue
        follows_bubble = bool(items) and 0 <= top - items[-1]["bottom"] <= 22
        if (top == 0 or follows_bubble) and height <= 32 and x1 - x0 <= 120 and x0 < .15 * chat.width and whites / max(area, 1) < .5:
            reactions.append((max(0, x0 - 2), max(0, top - 2), min(chat.width, x1 + 2), min(chat.height, bottom + 2)))
            continue
        if height < 32 and top != 0:
            continue
        central = x0 > chat.width * .25 and x1 < chat.width * .80
        system = (central and height < 85 and whites / max(area, 1) < .12)
        system |= height < 45 and x1 - x0 > chat.width * .8 and whites / max(area, 1) < .12
        if system:
            systems.append((max(0, x0 - 3), max(0, top - 3), min(chat.width, x1 + 3), min(chat.height, bottom + 3)))
            continue
        if area < 100:
            continue
        light_bounds = white.crop((left, top, chat.width, bottom)).getbbox()
        if light_bounds and whites / max(area, 1) > .5:
            x1 = light_bounds[2] + left
        outgoing_green = green_mask.crop((left, top, chat.width, bottom)).getbbox()
        outgoing = x0 > .55 * chat.width
        body_left = left
        if outgoing and outgoing_green and outgoing_green[0] + left > .55 * chat.width:
            body_left = outgoing_green[0] + left
            x1 = outgoing_green[2] + left
            outgoing = True
        elif outgoing:
            body_left = light_bounds[0] + left if light_bounds else x0
        if outgoing:
            read_receipts.append((max(0, body_left - 180), top, max(0, body_left - 4), max(top, bottom - 24)))
        avatar = foreground.crop((0, max(0, top - 24), left, max(1, top - 4)))
        author_present = top >= 25 and avatar.histogram()[255] > 120
        items.append({"top": top, "bottom": bottom, "right": x1,
                      "body_left": body_left, "side": "outgoing" if outgoing else "incoming",
                      "author_present": author_present, "partial_top": top == 0,
                      "partial_bottom": bottom == chat.height,
                      "kind": "text" if whites / max(area, 1) > .5 or outgoing_green and outgoing else "media"})
    cleaned = chat.copy()
    draw = ImageDraw.Draw(cleaned)
    for box in systems + reactions + read_receipts:
        draw.rectangle(box, fill=background)
    for item in items:
        top, bottom, right = item["top"], item["bottom"], item["right"]
        item["body"] = cleaned.crop((item["body_left"], top, max(item["body_left"] + 1, right), bottom))
        if item["side"] == "outgoing":
            footer = cleaned.crop((max(0, item["body_left"] - 180), max(top, bottom - 22),
                                   max(1, item["body_left"] - 4), min(chat.height, bottom + 6)))
        else:
            footer = cleaned.crop((min(chat.width - 1, right + 4), max(top, bottom - 32),
                                   min(chat.width, max(right + 5, right + 170)), min(chat.height, bottom + 6)))
        item["time"] = trim_ink(footer, background)
        header_top = max(0, top - 44) if item["author_present"] else max(0, top - 3)
        item["block"] = cleaned.crop((0, header_top, cleaned.width, min(cleaned.height, bottom + 6)))
        item["author"] = cleaned.crop((0, max(0, top - 44), min(650, chat.width), top)) if item["author_present"] else None
        item["source_crop"] = [crop_box[0], crop_box[1] + header_top, crop_box[2], crop_box[1] + min(chat.height, bottom + 6)]
    return items, {"background": background, "viewport": crop_box,
                   "ignored_system_regions": systems, "ignored_reaction_regions": reactions,
                   "ignored_ad_regions": ad_regions, "ignored_edge_remnants": edge_remnants, "size": chat.size}


def same_message(old, new):
    a, b = old["body"], new["body"]
    if abs(a.width - b.width) > 3:
        return False
    if abs(a.height - b.height) <= 3:
        # Exclude the rounded border. Real text changes must not be diluted by a
        # large uniform white background.
        w, h = min(a.width, b.width), min(a.height, b.height)
        box = (min(18, w // 4), min(3, h // 4), max(w - 3, 1), max(h - 3, 1))
        if changed_pixels(a.crop(box), b.crop(box)) > 18:
            return False
        if old.get("time") is not None and new.get("time") is not None:
            if changed_pixels(old["time"], new["time"]) > 10:
                return False
        if old.get("author") is not None and new.get("author") is not None:
            author_a, author_b = old["author"], new["author"]
            # A banner can clip only the sender header while the bubble is full.
            # Compare the shared bottom-aligned header instead of duplicating it.
            shared_height = min(author_a.height, author_b.height)
            if shared_height < 12:
                return False
            author_a = author_a.crop((0, author_a.height - shared_height, author_a.width, author_a.height))
            author_b = author_b.crop((0, author_b.height - shared_height, author_b.width, author_b.height))
            if changed_pixels(author_a, author_b) > 100:
                return False
        return True
    # A viewport-edge fragment can match the prefix/suffix of an earlier full
    # message; the earlier full pixels remain canonical after a recall/scroll.
    if new.get("partial_top") and b.height < a.height:
        a = a.crop((0, a.height - b.height, a.width, a.height))
    elif old.get("partial_top") and a.height < b.height:
        b = b.crop((0, b.height - a.height, b.width, b.height))
    elif new.get("partial_bottom") and b.height < a.height:
        a = a.crop((0, 0, a.width, b.height))
    elif old.get("partial_bottom") and a.height < b.height:
        b = b.crop((0, 0, b.width, a.height))
    else:
        return False
    w, h = min(a.width, b.width), min(a.height, b.height)
    if h < 12:
        return False
    box = (min(18, w // 4), 2, max(1, w - 3), max(3, h - 2))
    return changed_pixels(a.crop(box), b.crop(box)) <= 18


def ordered_matches(old, new, priorities=None):
    """Monotone sequence alignment keeps repeated short messages as occurrences."""
    rows, cols = len(old), len(new)
    priorities = priorities if priorities is not None else [1] * rows
    matches = [[same_message(a, b) for b in new] for a in old]
    dp = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            dp[i][j] = max(dp[i + 1][j], dp[i][j + 1], priorities[i] + dp[i + 1][j + 1] if matches[i][j] else 0)
    result, i, j = {}, 0, 0
    while i < rows and j < cols:
        if matches[i][j] and dp[i][j] == priorities[i] + dp[i + 1][j + 1]:
            result[j] = i
            i, j = i + 1, j + 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return result


def save_png(path, image):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        image.save(temporary, format="PNG")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class MessageArchive:
    """Never delete historical messages when later screenshots remove them."""
    def __init__(self, root, active_from=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "archive.json"
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {"schema": 1, "active_from": active_from if active_from is not None else window_start(time.time()),
                          "records": [], "order": [], "previous_ids": [], "last_source": "",
                          "next_id": 1, "frames": 0, "ignored_system_regions": 0}
        self.cache = OrderedDict()
        self.by_id = {record["id"]: record for record in self.state["records"]}
        if not self.path.exists():
            self.persist()

    def persist(self):
        atomic_json(self.path, self.state)

    def load(self, identifier):
        if identifier in self.cache:
            self.cache.move_to_end(identifier)
            return self.cache[identifier]
        record = self.by_id[identifier]
        result = {key: record[key] for key in ("partial_top", "partial_bottom", "kind")}
        for key in ("body", "block", "time", "author"):
            name = record["assets"].get(key)
            with Image.open(self.root / name) if name else _NoImage() as image:
                result[key] = image.convert("RGB") if image else None
        self.cache[identifier] = result
        while len(self.cache) > 160:
            self.cache.popitem(last=False)
        return result

    def store_assets(self, identifier, revision, item):
        assets = {}
        for key in ("body", "block", "time", "author"):
            if item.get(key) is not None:
                relative = f"assets/{identifier:08d}_v{revision}_{key}.png"
                save_png(self.root / relative, item[key])
                assets[key] = relative
        return assets

    def ingest(self, source, timestamp=None):
        source = Path(source)
        timestamp = capture_time(source) if timestamp is None else timestamp
        key = source.parent.name + "/" + source.name
        with Image.open(source) as image:
            items, details = observations(image.convert("RGB"))
        previous_ids = set(self.state["previous_ids"])
        old_ids = self.state["order"][-160:]
        old = [self.load(identifier) for identifier in old_ids]
        matches = ordered_matches(old, items, [5 if identifier in previous_ids else 1 for identifier in old_ids])
        current_ids, new_ids, upgrades = [], [], []
        bucket = window_start(timestamp) if timestamp >= self.state["active_from"] else None
        for position, item in enumerate(items):
            if position in matches:
                identifier = old_ids[matches[position]]
                record = self.by_id[identifier]
                was_partial = record["partial_top"] or record["partial_bottom"]
                now_full = not (item["partial_top"] or item["partial_bottom"])
                if was_partial and now_full:
                    record["revision"] += 1
                    record["assets"] = self.store_assets(identifier, record["revision"], item)
                    record.update(partial_top=False, partial_bottom=False, source=str(source), source_crop=item["source_crop"])
                    if bucket is not None and bucket not in record["deliver_in"]:
                        record["deliver_in"].append(bucket)
                        record["supplement_in"].append(bucket)
                    self.cache.pop(identifier, None)
                    upgrades.append(identifier)
                record["last_seen"] = timestamp
                record["seen_count"] += 1
            else:
                identifier = self.state["next_id"]
                self.state["next_id"] += 1
                record = {"id": identifier, "revision": 1, "source": str(source),
                          "source_crop": item["source_crop"], "first_seen": timestamp, "last_seen": timestamp,
                          "seen_count": 1, "partial_top": item["partial_top"], "partial_bottom": item["partial_bottom"],
                          "kind": item["kind"], "side": item["side"], "background": list(details["background"]),
                          "deliver_in": [bucket] if bucket is not None else [], "supplement_in": [],
                          "assets": self.store_assets(identifier, 1, item)}
                self.state["records"].append(record)
                self.by_id[identifier] = record
                # Previously unseen old content above a known anchor belongs
                # before that anchor; ordinary new messages append at the bottom.
                next_known = next((old_ids[matches[j]] for j in range(position + 1, len(items)) if j in matches), None)
                if next_known in self.state["order"]:
                    self.state["order"].insert(self.state["order"].index(next_known), identifier)
                else:
                    self.state["order"].append(identifier)
                new_ids.append(identifier)
            current_ids.append(identifier)
        self.state["previous_ids"] = current_ids
        self.state["last_source"] = key
        self.state["frames"] += 1
        self.state["ignored_system_regions"] += len(details["ignored_system_regions"])
        self.state["ignored_reaction_regions"] = self.state.get("ignored_reaction_regions", 0) + len(details["ignored_reaction_regions"])
        self.state["ignored_ad_regions"] = self.state.get("ignored_ad_regions", 0) + len(details["ignored_ad_regions"])
        self.persist()
        return {"source": str(source), "new_ids": new_ids, "upgraded_ids": upgrades,
                "current_ids": current_ids, "ignored_system_regions": details["ignored_system_regions"],
                "ignored_reaction_regions": details["ignored_reaction_regions"],
                "ignored_ad_regions": details["ignored_ad_regions"], "viewport": details["viewport"]}

    def identifiers_for(self, bucket):
        return [identifier for identifier in self.state["order"] if bucket in self.by_id[identifier]["deliver_in"]]

    def render(self, identifiers, folder, stem, max_height=9000):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        blocks, warnings = [], []
        for identifier in identifiers:
            record = self.by_id[identifier]
            image = self.load(identifier)["block"]
            if record["partial_top"] or record["partial_bottom"]:
                warnings.append(f"Message {identifier}: viewport-edge fragment retained; full content not yet captured")
                # Do not invent pixels hidden in the source. Make the limitation
                # visible in the image as well as in its provenance manifest.
                try:
                    font = ImageFont.truetype(r"C:\Windows\Fonts\msjh.ttc", 24)
                    label = "原始截圖邊緣已截斷：以下保留可見內容"
                except OSError:
                    font = ImageFont.load_default()
                    label = "Source image clipped: visible content retained"
                marked = Image.new("RGB", (image.width, image.height + 44), (37, 41, 51))
                ImageDraw.Draw(marked).text((20, 8), label, font=font, fill=(238, 191, 105))
                marked.paste(image, (0, 44))
                image = marked
            # An oversized message is preserved in ordered, non-overlapping pieces.
            for y in range(0, image.height, max_height - 48):
                blocks.append((identifier, image.crop((0, y, image.width, min(image.height, y + max_height - 48)))))
        groups, current, height = [], [], 24
        for block in blocks:
            if current and height + block[1].height + 24 > max_height:
                groups.append(current)
                current, height = [], 24
            current.append(block)
            height += block[1].height + 24
        if current:
            groups.append(current)
        pages = []
        for number, group in enumerate(groups, start=1):
            width = max(image.width for _, image in group)
            page = Image.new("RGB", (width, sum(image.height for _, image in group) + 24 * (len(group) + 1)),
                             tuple(self.by_id[group[0][0]]["background"]))
            y = 24
            for _, image in group:
                page.paste(image, (0, y))
                y += image.height + 24
            path = folder / f"{stem}_part{number:02d}.png"
            save_png(path, page)
            if path.stat().st_size > 45 * 1024 * 1024:
                raise ValueError("Long screenshot exceeds safe document upload size; raw data and rendered page retained")
            pages.append({"path": str(path), "message_ids": [identifier for identifier, _ in group],
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": list(page.size)})
        return pages, warnings


class _NoImage:
    def __enter__(self):
        return None

    def __exit__(self, *_):
        return False


class HalfHourDispatcher:
    """Capture-time windows [hh:00,hh:30), [hh:30,next hh:00), persisted across restart."""
    def __init__(self, captures, output, token="", chat_id="", active_from=None, send_enabled=True, channel_name=""):
        self.channel_name = channel_name
        self.captures, self.output = Path(captures), Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        # The previous archive contains ad/message hybrid observations. Start a
        # clean comparison context on upgrade without deleting historical assets,
        # sent manifests, receipts, or raw captures. Earlier sources seed context
        # only; initialize() does not deliver historical batches again.
        self.archive = MessageArchive(self.output / "state_adfilter_v1", active_from)
        self.state_path = self.output / "schedule_adfilter_v1.json"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            self.state = {"schema": 1, "next_window": self.archive.state["active_from"], "bootstrapped": False}
        self.outbox = TelegramOutbox(self.output, token, chat_id, compare_scenes=False) if token and chat_id and send_enabled else None
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = ""
        self.processed_windows = 0

    def sources(self):
        return sorted(self.captures.glob("????-??-??/*.png"))

    def initialize(self):
        if self.state["bootstrapped"]:
            return
        # Earlier frames establish context only. Do not mass-send historical raw
        # screenshots when migrating from the old immediate-upload version.
        previous = [path for path in self.sources() if capture_time(path) < self.archive.state["active_from"]]
        for path in previous[-12:]:
            key = path.parent.name + "/" + path.name
            if key > self.archive.state["last_source"]:
                self.ingest_safely(path)
        self.state["bootstrapped"] = True
        atomic_json(self.state_path, self.state)

    def queue_manifest(self, manifest_path, manifest):
        if not self.outbox or not manifest["pages"]:
            return
        start, end = datetime.fromtimestamp(manifest["start"]), datetime.fromtimestamp(manifest["end"])
        for index, page in enumerate(manifest["pages"], start=1):
            path = Path(page["path"])
            if not path.exists():
                raise FileNotFoundError(f"Rendered batch page missing: {path}")
            if not self.outbox.has_receipt(path, page["sha256"]):
                caption = f"LINE 對話備份｜{start:%Y-%m-%d %H:%M}–{end:%H:%M}｜{index}/{len(manifest['pages'])}"
                if self.channel_name:
                    caption = self.channel_name + "｜" + caption
                if manifest["warnings"]:
                    caption += "\n含邊緣片段或處理警告；完整原圖與報告保留在本機。"
                self.outbox.queue(path, caption=caption)
        if all(self.outbox.has_receipt(Path(page["path"]), page["sha256"]) for page in manifest["pages"]):
            manifest["status"] = "sent"
            atomic_json(manifest_path, manifest)

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.initialize()
        # Seal closed windows first from their own frames. Later recall snapshots
        # update the archive but can never overwrite an already-rendered batch.
        snapshots = self.sources()
        cursor = self.archive.state["last_source"]
        new = [p for p in snapshots if p.parent.name + "/" + p.name > cursor and capture_time(p) <= now]
        position = 0
        while self.state["next_window"] + 1800 + 2 <= now:
            start = self.state["next_window"]
            end = start + 1800
            while position < len(new) and capture_time(new[position]) < end:
                self.ingest_safely(new[position])
                position += 1
            date = datetime.fromtimestamp(start)
            folder = self.output / date.strftime("%Y-%m-%d")
            stem = date.strftime("%H%M") + "-" + datetime.fromtimestamp(end).strftime("%H%M")
            manifest_path = folder / f"{stem}.batch.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            else:
                identifiers = self.archive.identifiers_for(start)
                pages, warnings = self.archive.render(identifiers, folder, stem)
                warnings += [f"Message {identifier}: completed version of an earlier captured fragment"
                             for identifier in identifiers if start in self.archive.by_id[identifier].get("supplement_in", [])]
                for issues in ("source_errors", "skipped_sources"):
                    warnings += [message for key, message in self.state.get(issues, {}).items()
                                 if start <= capture_time(self.captures / key) < end]
                manifest = {"start": start, "end": end, "message_ids": identifiers, "pages": pages,
                            "warnings": warnings, "status": "ready" if pages else "empty",
                            "created_at": time.time(), "policy": "earliest-captured-message; omit system notices"}
                atomic_json(manifest_path, manifest)
                LOG.info("Half-hour window %s: %s messages, %s long screenshot pages", stem, len(identifiers), len(pages))
            self.queue_manifest(manifest_path, manifest)
            self.state["next_window"] = end
            atomic_json(self.state_path, self.state)
            self.processed_windows += 1
        for path in new[position:]:
            self.ingest_safely(path)
        # Recover ready pages after a crash between rendering, queueing and upload.
        for manifest_path in self.output.glob("????-??-??/*.batch.json"):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") == "ready":
                self.queue_manifest(manifest_path, manifest)
        self.last_error = ""
        return {"source_error_count": len(self.state.get("source_errors", {})),
                "latest_source_error": next(reversed(self.state.get("source_errors", {}).values()), ""),
                "next_window_end": self.state["next_window"] + 1800,
                "archive_messages": len(self.archive.state["records"]), "processed_windows": self.processed_windows}

    def ingest_safely(self, path):
        try:
            return self.archive.ingest(path)
        except NonChatFrame as exc:
            key = path.parent.name + "/" + path.name
            self.state.setdefault("skipped_sources", {})[key] = f"{key}: {exc}; original retained"
            self.archive.state["last_source"] = key
            self.archive.persist()
            atomic_json(self.state_path, self.state)
            LOG.warning("Skipped invalid chat frame: %s: %s", key, exc)
        except (OSError, ValueError) as exc:
            key = path.parent.name + "/" + path.name
            message = f"{key}: {type(exc).__name__}: {exc}; original retained"
            self.state.setdefault("source_errors", {})[key] = message
            self.archive.state["last_source"] = key
            self.archive.persist()
            atomic_json(self.state_path, self.state)
            LOG.error("Cannot parse screenshot for long image: %s", message)

    def run(self):
        if self.outbox:
            self.outbox.start()
        while not self.stop_event.is_set():
            try:
                status = self.tick()
                status.update(updated_at=time.time(), error="", mode="half_hour_long_screenshot",
                              uploads=self.outbox.delivered if self.outbox else 0,
                              uploader_error=self.outbox.last_error if self.outbox else "disabled")
                atomic_json(self.output / "batch-status.json", status)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOG.error("Long screenshot batch processing failed; originals preserved\n%s", safe_error(exc))
                try:
                    atomic_json(self.output / "batch-status.json", {"updated_at": time.time(), "error": self.last_error})
                except OSError:
                    pass
            self.stop_event.wait(5)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="HalfHourDispatcher", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join()
        if self.outbox:
            self.outbox.stop()


def preview_longshots(sources, output):
    """Explicit offline preview; never queues ordinary Telegram delivery."""
    sources = sorted({Path(path).resolve() for path in sources}, key=capture_time)
    if not sources:
        raise ValueError("No screenshots supplied")
    folder = Path(output) / ("validation_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    archive = MessageArchive(folder / "state", window_start(capture_time(sources[0])))
    frames = [archive.ingest(source) for source in sources]
    pages, warnings = archive.render(archive.state["order"], folder, "all_messages")
    intervals = []
    buckets = sorted({bucket for record in archive.state["records"] for bucket in record["deliver_in"]})
    for bucket in buckets:
        ids = archive.identifiers_for(bucket)
        start, end = datetime.fromtimestamp(bucket), datetime.fromtimestamp(bucket + 1800)
        stem = start.strftime("%Y%m%d_%H%M") + "-" + end.strftime("%H%M")
        window_pages, notes = archive.render(ids, folder / "windows", stem)
        intervals.append({"start": bucket, "end": bucket + 1800, "message_ids": ids,
                          "pages": window_pages, "warnings": notes})
    report = {"preview_only": True, "sources": [str(source) for source in sources],
              "message_count": len(archive.state["records"]),
              "ignored_system_regions": archive.state["ignored_system_regions"],
              "output": str(folder), "pages": pages, "intervals": intervals,
              "warnings": warnings, "frames": frames, "messages": archive.state["records"]}
    atomic_json(folder / "validation-report.json", report)
    return report
