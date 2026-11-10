import html
import json
import re


def seconds(text):
    parts = text.strip().replace(",", ".").split(":")
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def parse(text):
    """Read SRT, WebVTT or YouTube JSON3 without discarding cue timestamps."""
    text = text.lstrip("\ufeff")
    if text.lstrip().startswith("{"):
        obj = json.loads(text)
        segments = []
        for event in obj.get("events", []):
            value = "".join(s.get("utf8", "") for s in event.get("segs", [])).strip()
            if value:
                start = event.get("tStartMs", 0) / 1000
                segments.append(dict(start=start, end=start + event.get("dDurationMs", 1000) / 1000, text=value))
        return normalize(segments)
    segments = []
    pattern = r"((?:\d+:)?\d{2}:\d{2}[.,]\d+)\s*-->\s*((?:\d+:)?\d{2}:\d{2}[.,]\d+)[^\n]*\n(.*?)(?=\n\s*\n|\Z)"
    for match in re.finditer(pattern, text.replace("\r\n", "\n"), re.S):
        value = html.unescape(re.sub(r"<[^>]*>", "", match[3]))
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            segments.append(dict(start=seconds(match[1]), end=seconds(match[2]), text=value))
    return normalize(segments)


def normalize(segments):
    result = []
    for cue in sorted(segments, key=lambda s: s["start"]):
        cue = dict(start=max(0, float(cue["start"])), end=float(cue["end"]), text=str(cue["text"]).strip())
        if not cue["text"] or cue["end"] <= cue["start"]:
            continue
        if result and cue["start"] <= result[-1]["end"] + .15:
            previous = result[-1]
            if cue["text"] == previous["text"]:
                previous["end"] = max(previous["end"], cue["end"])
                continue
            # Rolling captions repeat the preceding line; retain only new words.
            if cue["text"].startswith(previous["text"]):
                cue["text"] = cue["text"][len(previous["text"]):].strip()
                if not cue["text"]:
                    continue
        result.append(cue)
    return result


def stamp(value):
    ms = max(0, round(float(value) * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def srt(segments):
    return "\n\n".join(f"{i}\n{stamp(s['start'])} --> {stamp(s['end'])}\n{s['text']}" for i, s in enumerate(segments, 1)) + "\n"


def timestamped_text(segments):
    return "\n\n".join(f"[{stamp(cue['start'])} --> {stamp(cue['end'])}]\n{cue['text']}" for cue in segments)


def chunks(segments, limit=5500):
    current, length = [], 0
    for i, segment in enumerate(segments):
        item = dict(id=i, **segment)
        if current and length + len(segment["text"]) > limit:
            yield current
            current, length = [], 0
        current.append(item)
        length += len(segment["text"]) + 50
    if current:
        yield current


def language_code(code):
    parts = (code or "").lower().replace("_", "-").split("-")
    primary = parts[1] if parts[0] == "ai" and len(parts) > 1 else parts[0]
    if primary in {"zh", "zho", "chi", "cmn", "yue", "chs", "cht"}:
        return "zh"
    return "en" if primary in {"en", "eng"} else "original"


def language(segments):
    text = " ".join(s["text"] for s in segments)[:4000]
    has_kana = re.search(r"[\u3040-\u30ff]", text)
    if not has_kana and len(re.findall(r"[\u4e00-\u9fff]", text)) > len(text) * .15:
        return "zh"
    # Untagged subtitles need positive evidence of English before translation.
    words = re.findall(r"\b[a-z]+\b", text.lower())
    common = {"the", "of", "and", "to", "is", "are", "this", "that", "with", "from", "you", "your", "welcome", "hello", "thanks"}
    return "en" if sum(word in common for word in words) >= 2 or {"hello", "thanks"}.intersection(words) else "original"
