"""Pure helpers for mining videos: YouTube ids, timestamps, WebVTT captions and clip sections. No network, no
subprocess; `tests/test_mining.py` covers it. The scripts next to this file import it by path."""
from __future__ import annotations

import html
import re

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
_CUE = re.compile(r"^(\d{1,2}:)?\d{2}:\d{2}\.\d{3}\s+-->\s+(\d{1,2}:)?\d{2}:\d{2}\.\d{3}")
_TAG = re.compile(r"<[^>]*>")


def video_id(s: str) -> str:
    """The 11-character id from a watch/youtu.be/shorts/live URL, or a bare id. ValueError otherwise."""
    s = s.strip()
    if _ID.match(s):
        return s
    m = _URL.search(s)
    if not m:
        raise ValueError(f"not a YouTube video URL or id: {s!r}")
    return m.group(1)


def parse_hms(s: str | float | int) -> float:
    """'1:02:05', '12:34', '754' or a number -> seconds."""
    if isinstance(s, (int, float)):
        return float(s)
    parts = s.strip().split(":")
    if not all(p.replace(".", "", 1).isdigit() for p in parts) or len(parts) > 3:
        raise ValueError(f"not a timestamp: {s!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def hms(seconds: float) -> str:
    """Seconds -> 'mm:ss' under an hour, 'h:mm:ss' over."""
    t = int(seconds)
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fname(seconds: float) -> str:
    """Seconds -> 'h-mm-ss', a sortable file stem."""
    t = int(seconds)
    return f"{t // 3600}-{(t % 3600) // 60:02d}-{t % 60:02d}"


def _clean(line: str) -> str:
    return " ".join(html.unescape(_TAG.sub("", line)).split())


def parse_vtt(text: str) -> list[tuple[float, str]]:
    """Cues -> [(start_seconds, line)], each spoken line once.

    YouTube's auto-captions roll: every cue restates the previous line and adds one, with `<c>` word timings inside.
    Tags and entities are stripped, filler cues dropped, and a line equal to the last one emitted is skipped. Manual
    captions (no tags, no repeats) pass through, a multi-line cue as one line."""
    out: list[tuple[float, str]] = []
    last = ""
    start = None
    block: list[str] = []

    def flush():
        nonlocal last
        if start is None:
            return
        lines = [_clean(l) for l in block]
        lines = [l for l in lines if l]
        if not lines:
            return
        fresh = [l for l in lines if l != last]
        if not fresh:
            return
        if len(fresh) == len(lines) and len(lines) > 1 and not any("<c>" in l for l in block):
            fresh = [" ".join(lines)]                   # a manual multi-line cue is one line
        for l in fresh:
            if l != last:
                out.append((start, l))
                last = l

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        m = _CUE.match(line)
        if m:
            flush()
            start = parse_hms(line.split("-->")[0].strip())
            block = []
        elif line == "":                                # a blank line ends a cue; a space-only line is filler
            flush()
            start, block = None, []
        elif start is not None:
            block.append(line)
    flush()
    return out


def merge(lines: list[tuple[float, str]], window: float) -> list[tuple[float, str]]:
    """Join consecutive lines into blocks no longer than `window` seconds, keyed by the first line's time."""
    if window <= 0 or not lines:
        return list(lines)
    out: list[tuple[float, str]] = []
    t0, buf = lines[0][0], [lines[0][1]]
    for t, l in lines[1:]:
        if t - t0 < window:
            buf.append(l)
        else:
            out.append((t0, " ".join(buf)))
            t0, buf = t, [l]
    out.append((t0, " ".join(buf)))
    return out


def sections(times: list[float], pad: float = 2, gap: float = 60) -> list[tuple[float, float]]:
    """Group timestamps within `gap` seconds of each other into (start, end) clips, each padded by `pad`."""
    out: list[tuple[float, float]] = []
    for t in sorted(times):
        if out and t - out[-1][1] + pad <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], t + pad))
        else:
            out.append((max(0.0, t - pad), t + pad))
    return out
