"""Sample frames from a fetched video for mining: mining/<id>/frames/*.jpg.

    uv run python -I tools/mine/frames.py ID --at 12:34 [--at 17:55 ...] [--pad 2] [--width 1280]
    uv run python -I tools/mine/frames.py ID --from 9:45 --to 12:00 --step 15 [--sheet] [--width 1280]
    uv run python -I tools/mine/frames.py ID --full            # one 720p download; later calls cut it locally

Moments within 60 s share one section clip, fetched with yt-dlp --download-sections (video only, <=720p) into
mining/<id>/media/ (gitignored). A frame is a JPEG named by its time (h-mm-ss.jpg). A range makes one frame per
--step seconds, or with --sheet one contact sheet of 640px cells in rows of 4, read left to right from --from in
--step increments. Existing clips and frames are reused; the JPEG paths are printed."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
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
FORMAT = "bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/b"


def ytdlp() -> list[str]:
    exe = shutil.which("yt-dlp")
    return [exe] if exe else ["uvx", "yt-dlp"]


def ffmpeg() -> str:
    return shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, check=True)


class Video:
    def __init__(self, vid: str, root: Path):
        self.vid = vid
        self.dir = root / vid
        self.media = self.dir / "media"
        self.frames = self.dir / "frames"
        self.media.mkdir(parents=True, exist_ok=True)
        self.frames.mkdir(exist_ok=True)
        meta = self.dir / "metadata.json"
        self.url = json.loads(meta.read_text()).get("url") if meta.exists() else None
        self.url = self.url or f"https://www.youtube.com/watch?v={vid}"

    @property
    def full(self) -> Path:
        return self.media / f"{self.vid}.mp4"

    def fetch_full(self) -> Path:
        if not self.full.exists():
            run(ytdlp() + ["--no-playlist", "-f", FORMAT, "--merge-output-format", "mp4",
                           "-P", str(self.media), "-o", "%(id)s.%(ext)s", self.url])
        return self.full

    def clip(self, start: float, end: float) -> tuple[Path, float]:
        """A file covering [start, end] and the time its t=0 corresponds to."""
        if self.full.exists():
            return self.full, 0.0
        p = self.media / f"clip_{int(start)}-{int(end)}.mp4"
        if not p.exists():
            run(ytdlp() + ["--no-playlist", "-f", FORMAT, "--merge-output-format", "mp4",
                           "--download-sections", f"*{int(start)}-{int(end)}", "--force-keyframes-at-cuts",
                           "-P", str(self.media), "-o", p.name, self.url])
        return p, float(int(start))

    def frame(self, t: float, src: Path, origin: float, width: int) -> Path:
        out = self.frames / f"{vtt.fname(t)}.jpg"
        if not out.exists():
            run([ffmpeg(), "-loglevel", "error", "-y", "-ss", f"{t - origin:.3f}", "-i", str(src),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)])
        return out

    def series(self, t0: float, t1: float, step: float, src: Path, origin: float, width: int) -> list[Path]:
        n = int((t1 - t0) // step) + 1
        want = [self.frames / f"{vtt.fname(t0 + k * step)}.jpg" for k in range(n)]
        if all(p.exists() for p in want):
            return want
        tmp = self.frames / "tmp_%03d.jpg"
        run([ffmpeg(), "-loglevel", "error", "-y", "-ss", f"{t0 - origin:.3f}", "-t", f"{t1 - t0 + 0.5:.3f}",
             "-i", str(src), "-vf", f"fps=1/{step},scale={width}:-2", "-q:v", "4", "-frames:v", str(n), str(tmp)])
        made = sorted(self.frames.glob("tmp_*.jpg"))
        for k, p in enumerate(made):
            p.replace(self.frames / f"{vtt.fname(t0 + k * step)}.jpg")
        return [p for p in want if p.exists()]

    def sheet(self, t0: float, t1: float, step: float, src: Path, origin: float) -> Path:
        n = int((t1 - t0) // step) + 1
        out = self.frames / f"sheet_{vtt.fname(t0)}_{vtt.fname(t1)}_s{int(step)}.jpg"
        if not out.exists():
            run([ffmpeg(), "-loglevel", "error", "-y", "-ss", f"{t0 - origin:.3f}", "-t", f"{t1 - t0 + 0.5:.3f}",
                 "-i", str(src), "-frames:v", "1",
                 "-vf", f"fps=1/{step},scale=640:-2,tile=4x{math.ceil(n / 4)}", "-q:v", "5", str(out)])
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("id")
    ap.add_argument("--at", action="append", default=[], help="a moment (mm:ss); repeatable")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--step", type=float, default=15)
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--pad", type=float, default=2)
    ap.add_argument("--gap", type=float, default=60)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--root", default="mining")
    a = ap.parse_args()

    v = Video(vtt.video_id(a.id), Path(a.root))
    made: list[Path] = []
    if a.full:
        print(v.fetch_full())
    if a.at:
        times = [vtt.parse_hms(t) for t in a.at]
        for s, e in vtt.sections(times, a.pad, a.gap):
            src, origin = v.clip(s, e)
            made += [v.frame(t, src, origin, a.width) for t in times if s <= t <= e]
    if a.start or a.end:
        if not (a.start and a.end):
            ap.error("--from and --to go together")
        t0, t1 = vtt.parse_hms(a.start), vtt.parse_hms(a.end)
        if t1 <= t0:
            ap.error("--to must be after --from")
        src, origin = v.clip(t0, t1)
        made += [v.sheet(t0, t1, a.step, src, origin)] if a.sheet else v.series(t0, t1, a.step, src, origin, a.width)
    for p in made:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
