"""Fetch a YouTube video's captions and metadata for mining: mining/<id>/metadata.json and transcript.txt.

    uv run python -I tools/mine/fetch_transcript.py URL_OR_ID [--lang en] [--merge SECONDS] [--root mining] [--force]

yt-dlp runs as a subprocess (PATH, else `uvx yt-dlp`). The raw .vtt lands in mining/<id>/media/ (gitignored); the
transcript is one `[mm:ss] line` per spoken line, deduped from YouTube's rolling auto-captions. Exit 3 when the video
has no English captions."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _lib(name: str):
    spec = importlib.util.spec_from_file_location(f"mine_{name}", HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vtt = _lib("vtt")


def ytdlp() -> list[str]:
    exe = shutil.which("yt-dlp")
    return [exe] if exe else ["uvx", "yt-dlp"]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=True, text=True, **kw)


def metadata(url: str) -> dict:
    out = run(ytdlp() + ["--dump-single-json", "--skip-download", "--no-playlist", url], capture_output=True).stdout
    info = json.loads(out)
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    kind = "manual" if any(k.startswith("en") for k in subs) else "auto" if any(k.startswith("en") for k in auto) else "none"
    return {
        "id": info["id"], "title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"), "upload_date": info.get("upload_date"), "url": info.get("webpage_url"),
        "view_count": info.get("view_count"), "captions": kind,
        "chapters": [{"start": c.get("start_time"), "end": c.get("end_time"), "title": c.get("title")}
                     for c in (info.get("chapters") or [])],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--merge", type=float, default=0, help="join lines into blocks of up to N seconds")
    ap.add_argument("--root", default="mining")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    vid = vtt.video_id(a.url)
    url = a.url if a.url.startswith("http") else f"https://www.youtube.com/watch?v={vid}"
    d = Path(a.root) / vid
    media = d / "media"
    media.mkdir(parents=True, exist_ok=True)
    meta_path, out_path = d / "metadata.json", d / "transcript.txt"

    if meta_path.exists() and not a.force:
        meta = json.loads(meta_path.read_text())
    else:
        meta = metadata(url)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    if out_path.exists() and not a.force:
        print(f"have {out_path} ({sum(1 for _ in out_path.open())} lines); --force to refetch")
        return 0
    if meta["captions"] == "none":
        print(f"{vid}: no {a.lang} captions (manual or auto). Sample frames by chapter instead.", file=sys.stderr)
        return 3

    subs = sorted(media.glob(f"{vid}.{a.lang}*.vtt"))
    if not subs or a.force:
        run(ytdlp() + ["--skip-download", "--no-playlist", "--write-subs", "--write-auto-subs",
                       "--sub-langs", a.lang, "--sub-format", "vtt",
                       "-P", str(media), "-o", "%(id)s.%(ext)s", url])
        subs = sorted(media.glob(f"{vid}.{a.lang}*.vtt"))
    if not subs:
        print(f"{vid}: yt-dlp wrote no .vtt into {media}", file=sys.stderr)
        return 3
    exact = [s for s in subs if s.name == f"{vid}.{a.lang}.vtt"]
    sub = (exact or subs)[0]

    lines = vtt.parse_vtt(sub.read_text(encoding="utf-8", errors="replace"))
    if a.merge:
        lines = vtt.merge(lines, a.merge)
    chapters = " | ".join(f"{vtt.hms(c['start'] or 0)} {c['title']}" for c in meta["chapters"]) or "none"
    header = [
        f"# {meta['title']} — {meta['channel']} — {vtt.hms(meta['duration'] or 0)} — {meta['url']}",
        f"# captions: {meta['captions']} ({sub.name})",
        f"# chapters: {chapters}",
    ]
    out_path.write_text("\n".join(header + [f"[{vtt.hms(t)}] {l}" for t, l in lines]) + "\n")
    print(f"{meta_path}\n{out_path} ({len(lines)} lines, captions {meta['captions']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
