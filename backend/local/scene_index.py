"""Build a resumable, low-resolution scene index for one source video."""
from bisect import bisect_left
from io import BytesIO
import math
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

from .resources import reserve_disk
from .storage import atomic_bytes, atomic_json, fingerprint, read_checkpoint

WIDTH, HEIGHT, FPS = 288, 162, 2
CHUNK_SECONDS = 240
LAPLACIAN = ImageFilter.Kernel((3, 3), (0, -1, 0, -1, 4, -1, 0, -1, 0), scale=1, offset=128)


def _valid_scene(item):
    return (isinstance(item, dict) and all(type(item.get(key)) in (int, float) and math.isfinite(item[key])
                                         for key in ("start", "end", "time", "quality"))
            and 0 <= item["start"] <= item["time"] < item["end"] and item["quality"] >= 0
            and type(item.get("card")) is bool and item.get("thumb") == f'{round(item["time"] * 1000)}.jpg')


def _content(image):
    gray = image.convert("L")
    width, height = gray.size
    return gray.crop((width // 25, height // 18, width - width // 25, height * 5 // 6))


def _title_card(content):
    hist = content.histogram()
    count = content.width * content.height
    return sum(hist[:48]) / count >= .94 and .0005 <= sum(hist[185:]) / count <= .12


def frame_metrics(image):
    content = _content(image)
    if min(content.size) < 3:
        return 0, False
    edges = content.filter(LAPLACIAN)
    edges = edges.crop((1, 1, edges.width - 1, edges.height - 1))
    contrast = ImageStat.Stat(content).stddev[0]
    # Sparse bright text on dark slides is useful even with low global contrast.
    return ImageStat.Stat(edges).var[0] if contrast >= 1.5 else 0, _title_card(content)


def _scan(path, start, end, root, ffmpeg, check):
    scenes = []
    with TemporaryDirectory(dir=root) as tmp:
        raw = Path(tmp) / "frames.gray"
        frame_bytes = WIDTH * HEIGHT
        frame_count = math.ceil((end - start) * FPS)
        with reserve_disk(root, frame_count * frame_bytes):
            ffmpeg(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(path), "-map", "0:V:0",
                    "-an", "-t", str(end - start), "-vf",
                    f"fps={FPS},scale={WIDTH}:{HEIGHT},format=gray", "-pix_fmt", "gray", "-f", "rawvideo", str(raw)],
                   check, timeout=240)
        previous = None
        samples = []

        def finish(stop):
            # Compare stable frames near the start, middle and end of the scene.
            middle = (samples[0][0] + samples[-1][0]) / 2
            stable = samples[1:] if len(samples) > 2 else samples
            choices = (stable[0], min(stable, key=lambda sample: abs(sample[0] - middle)), stable[-1])
            timestamp, image, quality, card = max(
                ((time, frame, frame_metrics(frame)[0], is_card) for time, frame, is_card in choices),
                key=lambda sample: (sample[3], sample[2]))
            thumbnail = f"{round(timestamp * 1000)}.jpg"
            with BytesIO() as buffer:
                image.save(buffer, format="JPEG", quality=84)
                atomic_bytes(root / thumbnail, buffer.getvalue())
            scenes.append(dict(start=samples[0][0], end=stop, time=timestamp, quality=quality, card=card,
                               thumb=thumbnail))

        with raw.open("rb") as stream:
            for index in range(frame_count):
                if index % 32 == 0:
                    check()
                data = stream.read(frame_bytes)
                if len(data) != frame_bytes:
                    break
                image = Image.frombytes("L", (WIDTH, HEIGHT), data)
                timestamp = start + index / FPS
                card = _title_card(_content(image))
                if samples:
                    change = ImageStat.Stat(ImageChops.difference(previous, image)).mean[0]
                    elapsed = timestamp - samples[0][0]
                    if ((change >= 14 and elapsed >= 1.5) or card != samples[-1][2] or elapsed >= 25):
                        finish(timestamp)
                        samples = []
                samples.append((timestamp, image, card))
                previous = image
        if samples:
            finish(end)
    return scenes


def build(path, duration, cache, ffmpeg, check, progress=None):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("场景索引需要有效的视频时长")
    stat = path.stat()
    key = fingerprint("scene-index-v2", path.name, stat.st_size, stat.st_mtime_ns, duration)[:24]
    root = cache / key
    root.mkdir(parents=True, exist_ok=True)
    count = math.ceil(duration / CHUNK_SECONDS)
    scenes = []
    for piece in range(count):
        check()
        start = piece * CHUNK_SECONDS
        end = min(duration, start + CHUNK_SECONDS)
        checkpoint = root / f"part-{piece}.json"
        part = read_checkpoint(checkpoint, lambda value: isinstance(value, list) and all(
            _valid_scene(item) and start <= item["start"] < item["end"] <= end
            and (root / item["thumb"]).is_file() for item in value))
        if part is None:
            part = _scan(path, start, end, root, ffmpeg, check)
            atomic_json(checkpoint, part)
        scenes.extend(part)
        if progress:
            progress((piece + 1) * 100 / count)
    if scenes:
        atomic_json(cache / "index.json", dict(key=key, scenes=scenes))
    return SceneIndex(root, scenes)


def load(cache):
    manifest = read_checkpoint(cache / "index.json", lambda value: isinstance(value, dict)
                               and isinstance(value.get("scenes"), list)
                               and all(_valid_scene(item) for item in value["scenes"])
                               and isinstance(value.get("key"), str) and len(value["key"]) == 24
                               and all(character in "0123456789abcdef" for character in value["key"]))
    return SceneIndex(cache / manifest["key"], manifest["scenes"]) if manifest else None


class SceneIndex:
    def __init__(self, root, scenes):
        self.root = root
        self.scenes = [dict(scene, id=index + 1) for index, scene in enumerate(scenes)]
        self.times = [scene["time"] for scene in self.scenes]

    def local(self, anchor, radius, previous_radius, kind, exclude_times):
        low, high = bisect_left(self.times, anchor - radius), bisect_left(self.times, anchor + radius + .001)
        items = [scene for scene in self.scenes[low:high]
                 if all(abs(scene["time"] - time) >= .5 for time in exclude_times)]
        return sorted(items, key=lambda scene: (
            kind == "parameter" and scene["card"],
            previous_radius is not None and abs(scene["time"] - anchor) > previous_radius,
            -abs(scene["time"] - anchor), scene["quality"]), reverse=True)[:6]

    def shortlist(self, anchor, kind, exclude_times):
        nearby = self.local(anchor, 120, 24, kind, exclude_times)
        if not self.scenes:
            return nearby
        ranked = sorted((scene for scene in self.scenes
                         if all(abs(scene["time"] - time) >= .5 for time in exclude_times)),
                        key=lambda scene: (kind == "parameter" and scene["card"], -abs(scene["time"] - anchor)),
                        reverse=True)
        # Retain temporally distributed scenes as well as strong local and slide candidates.
        spread = self.scenes[::max(1, math.ceil(len(self.scenes) / 7))]
        by_id = {}
        for scene in [*nearby, *spread, *ranked[:25]]:
            if all(abs(scene["time"] - time) >= .5 for time in exclude_times):
                by_id.setdefault(scene["id"], scene)
        return sorted(list(by_id.values())[:25], key=lambda scene: scene["id"])

    def sheet(self, scenes, target):
        atomic_bytes(target, self.montage(scenes))
        return target

    def montage(self, scenes):
        columns = min(5, len(scenes))
        cell_width, cell_height = 270, 174
        image = Image.new("RGB", (columns * cell_width, math.ceil(len(scenes) / columns) * cell_height), "#18212b")
        draw = ImageDraw.Draw(image)
        for index, scene in enumerate(scenes):
            x, y = index % columns * cell_width, index // columns * cell_height
            with Image.open(self.root / scene["thumb"]) as thumbnail:
                thumbnail.thumbnail((cell_width - 8, cell_height - 24))
                image.paste(thumbnail, (x + 4, y + 4))
            seconds = int(scene["time"])
            draw.text((x + 5, y + cell_height - 18), f'{scene["id"]}  {seconds // 60:02d}:{seconds % 60:02d}', fill="white")
        with BytesIO() as buffer:
            image.save(buffer, format="JPEG", quality=88)
            return buffer.getvalue()
