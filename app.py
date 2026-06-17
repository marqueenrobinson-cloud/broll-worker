"""
B-roll assembly worker.

Takes a shot list (each shot = a footage clip URL, a caption, and a duration),
trims + crops each clip to vertical 9:16, burns in the caption, stitches them
together, and returns one finished MP4.

This is silent + captioned (no voiceover) — the simplest reliable version.
Voiceover can be added later as a v2.

Run locally:  uvicorn app:app --host 0.0.0.0 --port 8000
Needs ffmpeg installed on the host.
"""

import os
import uuid
import shutil
import subprocess
import tempfile
import urllib.request

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List

app = FastAPI()

# Output dimensions for vertical short-form (9:16)
W, H = 1080, 1920


class Shot(BaseModel):
    text: str = ""          # caption to burn in
    videoUrl: str           # source footage clip
    seconds: float = 3.0    # how long this shot should run


class BuildRequest(BaseModel):
    shots: List[Shot]


def run(cmd):
    """Run a shell command, raise if it fails."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])
    return proc


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "broll-worker"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def wrap_caption(text, max_chars=28):
    """Wrap a caption into lines so it fits the vertical frame width."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= max_chars:
            cur = (cur + " " + w).strip()
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines[:3])  # cap at 3 lines so it never overflows vertically


def escape_caption(text):
    # Wrap first, then escape characters that break ffmpeg's drawtext filter
    text = wrap_caption(text)
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")
        .replace("%", "\\%")
    )


def build_clip(src, out, seconds, caption, work):
    """Trim to `seconds`, crop/scale to 9:16, burn in the caption."""
    cap = escape_caption(caption)
    # Scale to cover the frame, crop center, trim to length, add caption near bottom.
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},"
        f"drawtext=text='{cap}':"
        f"fontcolor=white:fontsize=54:box=1:boxcolor=black@0.55:boxborderw=22:"
        f"x=(w-text_w)/2:y=h-360:line_spacing=10"
    )
    run([
        "ffmpeg", "-y",
        "-t", str(seconds),
        "-i", src,
        "-vf", vf,
        "-an",                       # drop any source audio (silent)
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        out,
    ])


@app.get("/")
def health():
    return {"status": "ok", "service": "broll-worker"}


@app.post("/build")
def build(req: BuildRequest):
    if not req.shots:
        return JSONResponse({"error": "No shots provided."}, status_code=400)

    work = tempfile.mkdtemp(prefix="broll_")
    try:
        clip_paths = []
        for i, shot in enumerate(req.shots):
            if not shot.videoUrl:
                continue
            raw = os.path.join(work, f"raw_{i}.mp4")
            clip = os.path.join(work, f"clip_{i}.mp4")
            try:
                download(shot.videoUrl, raw)
                build_clip(raw, clip, max(2.0, shot.seconds), shot.text or "", work)
                clip_paths.append(clip)
            except Exception as e:
                # Skip a bad clip rather than failing the whole video
                print(f"shot {i} failed: {e}")
                continue

        if not clip_paths:
            return JSONResponse({"error": "No usable clips."}, status_code=422)

        # Concat list file
        listfile = os.path.join(work, "list.txt")
        with open(listfile, "w") as f:
            for c in clip_paths:
                f.write(f"file '{c}'\n")

        out_name = f"{uuid.uuid4().hex}.mp4"
        out_path = os.path.join(tempfile.gettempdir(), out_name)
        run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", listfile,
            "-c", "copy",
            out_path,
        ])

        return FileResponse(out_path, media_type="video/mp4", filename="faceless.mp4")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        shutil.rmtree(work, ignore_errors=True)

