from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from .config import BUNDLE_DIR, PROJECT_ROOT, Settings
from .models import MediaInfo

ProgressCb = Optional[Callable[[float], None]]  # receives seconds of output done

# Keep child processes from flashing a console window on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class FFmpegError(RuntimeError):
    def __init__(self, message: str, cmd: Sequence[str] = (), log: str = ""):
        super().__init__(message)
        self.cmd = list(cmd)
        self.log = log


class FFmpegMissing(FFmpegError):
    pass


class Cancelled(RuntimeError):
    """Raised when the user stops a running job."""


# Folders searched before PATH, so a packaged build carries its own FFmpeg and
# the machine it is copied to needs nothing installed. Order matters: the
# bundled copy is the one this build was tested against.
_LOCAL_TOOL_DIRS = (BUNDLE_DIR / "ffmpeg", PROJECT_ROOT / "ffmpeg", BUNDLE_DIR,
                    PROJECT_ROOT)


def _which(name: str) -> str:
    suffix = ".exe" if sys.platform == "win32" else ""
    for folder in _LOCAL_TOOL_DIRS:
        local = folder / f"{name}{suffix}"
        if local.is_file():
            return str(local)
    exe = shutil.which(name)
    if not exe:
        raise FFmpegMissing(
            f"{name} not found on PATH. Install FFmpeg and reopen the app."
        )
    return exe


def ffmpeg_path() -> str:
    return _which("ffmpeg")


def ffprobe_path() -> str:
    return _which("ffprobe")


def tool_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in ("ffmpeg", "ffprobe"):
        try:
            proc = subprocess.run(
                [_which(name), "-version"], capture_output=True, text=True,
                creationflags=_NO_WINDOW,
            )
            head = (proc.stdout or "").splitlines()
            out[name] = head[0] if head else "unknown"
        except FFmpegMissing as exc:
            out[name] = f"MISSING ({exc})"
    return out


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------
def _parse_fps(value: str | None) -> float:
    if not value:
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            d = float(den)
            return float(num) / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _rotation(video_stream: dict) -> int:
    tags = video_stream.get("tags") or {}
    for key in ("rotate", "Rotate"):
        if key in tags:
            try:
                return abs(int(float(tags[key]))) % 360
            except (TypeError, ValueError):
                pass
    for sd in video_stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return abs(int(float(sd["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    return 0


def probe(path: str | Path, count_frames: bool = False) -> MediaInfo:
    """Read stream/container facts for one media file.

    `count_frames` decodes the file to get an exact video frame count. Use it
    for short clips whose length has to be trusted to the frame (reactions);
    it is far too slow for a full-length input video.
    """
    path = Path(path)
    if not path.exists():
        raise FFmpegError(f"File not found: {path}")

    cmd = [
        ffprobe_path(), "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
    ]
    if count_frames:
        cmd += ["-count_frames"]
    cmd += [str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path.name}", cmd, proc.stderr)

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"Unreadable ffprobe output for {path.name}", cmd,
                          str(exc)) from exc

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise FFmpegError(f"No video stream in {path.name}", cmd)

    fmt = data.get("format") or {}
    duration = 0.0
    for candidate in (fmt.get("duration"), video.get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    fps = _parse_fps(video.get("avg_frame_rate")) or _parse_fps(
        video.get("r_frame_rate"))

    # Rotation metadata means the displayed frame is transposed.
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if _rotation(video) in (90, 270):
        width, height = height, width

    try:
        size = int(fmt.get("size") or path.stat().st_size)
    except (TypeError, ValueError, OSError):
        size = 0

    video_duration = 0.0
    try:
        video_duration = float(video.get("duration") or 0.0)
    except (TypeError, ValueError):
        video_duration = 0.0

    frame_count = 0
    for key in ("nb_read_frames", "nb_frames"):
        try:
            frame_count = int(video.get(key) or 0)
        except (TypeError, ValueError):
            frame_count = 0
        if frame_count > 0:
            break

    return MediaInfo(
        path=path,
        duration=duration,
        width=width,
        height=height,
        fps=fps or 30.0,
        has_audio=audio is not None,
        vcodec=str(video.get("codec_name") or "?"),
        size_bytes=size,
        video_duration=video_duration,
        frame_count=frame_count,
    )


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------
_NOISE_PREFIXES = (
    "frame=", "fps=", "bitrate=", "total_size=", "out_time=", "dup_frames=",
    "drop_frames=", "speed=", "progress=", "stream_",
)


def run(cmd: Sequence[str], on_progress: ProgressCb = None,
        cancelled: Callable[[], bool] | None = None,
        label: str = "ffmpeg") -> str:
    """Run an ffmpeg command, streaming -progress output to `on_progress`.

    Returns the tail of the log. Raises FFmpegError on non-zero exit and
    Cancelled if the `cancelled` predicate goes true mid-run.
    """
    proc = subprocess.Popen(
        list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, creationflags=_NO_WINDOW,
    )
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line.startswith(("out_time_us=", "out_time_ms=")):
                if on_progress:
                    raw = line.split("=", 1)[1].strip()
                    try:
                        micros = float(raw)
                    except ValueError:
                        continue
                    # Both keys carry microseconds in ffmpeg's -progress output.
                    on_progress(max(0.0, micros / 1_000_000.0))
            elif not line.startswith(_NOISE_PREFIXES):
                tail.append(line)
                if len(tail) > 60:
                    del tail[0]
            if cancelled and cancelled():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise Cancelled(f"{label} cancelled")
    finally:
        if proc.stdout:
            proc.stdout.close()

    code = proc.wait()
    log = "\n".join(tail)
    if code != 0:
        raise FFmpegError(f"{label} failed (exit {code})", cmd, log)
    return log


# --------------------------------------------------------------------------
# encoder selection
# --------------------------------------------------------------------------
_HW_CANDIDATES = ("h264_nvenc", "h264_qsv", "h264_amf")
_encoder_cache: dict[str, str] = {}


def available_encoders() -> list[str]:
    proc = subprocess.run([ffmpeg_path(), "-hide_banner", "-encoders"],
                          capture_output=True, text=True,
                          creationflags=_NO_WINDOW)
    listed = proc.stdout or ""
    return [e for e in _HW_CANDIDATES if e in listed] + ["libx264"]


def _encoder_works(name: str) -> bool:
    """Listed is not the same as usable, so smoke-test a 6-frame encode."""
    cmd = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=0.2",
        "-c:v", name, "-frames:v", "6", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45,
                              creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def pick_encoder(settings: Settings, force_redetect: bool = False) -> str:
    """Fastest working H.264 encoder, hardware first."""
    if settings.encoder and settings.encoder != "auto":
        return settings.encoder
    if not force_redetect:
        if settings._detected_encoder:
            return settings._detected_encoder
        if "auto" in _encoder_cache:
            settings._detected_encoder = _encoder_cache["auto"]
            return settings._detected_encoder

    chosen = "libx264"
    for name in available_encoders():
        if name == "libx264" or _encoder_works(name):
            chosen = name
            break
    _encoder_cache["auto"] = chosen
    settings._detected_encoder = chosen
    return chosen


# --------------------------------------------------------------------------
# filter / encode argument construction
# --------------------------------------------------------------------------
def video_filter(settings: Settings) -> str:
    """Fit arbitrary input into an exact WxH 9:16 frame, SAR 1:1, CFR."""
    w, h, fps = settings.width, settings.height, settings.fps
    mode = (settings.fit_mode or "blur").lower()

    # lanczos, not the default bilinear: cropping 16:9 to 9:16 keeps a
    # 607px-wide column of a 1080p source, which is then scaled UP - and an
    # upscale is where the scaler choice actually shows.
    if mode == "crop":
        core = (f"scale={w}:{h}:force_original_aspect_ratio=increase"
                f":flags=lanczos,crop={w}:{h}")
    elif mode == "pad":
        core = (f"scale={w}:{h}:force_original_aspect_ratio=decrease"
                f":flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    else:
        # blur: filled background, whole source frame kept centred on top
        core = (
            "split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma=24[bg];"
            f"[fgsrc]scale={w}:{h}:force_original_aspect_ratio=decrease"
            f":flags=lanczos[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    return f"{core},fps={fps},setsar=1,format=yuv420p"


# preset -> (x264 crf, average kbps, peak kbps, VBV buffer kbps)
#
# Hardware encoders are driven by explicit bitrate rather than a quality
# target: their quality modes ignore -maxrate on some drivers, which produced
# 300 MB+ files for a 3-minute clip. Bitrate mode keeps the output size
# predictable (req 8), which matters more here than the last few percent of
# quality-per-byte.
#
# The kbps figures are calibrated at 720x1280 and scaled by pixel count for
# whatever frame is actually configured. Fixed numbers were tried first and
# quietly became a resolution ceiling: raising the frame to 1080x1920 while
# the bitrate stayed at 2500k spends 2.25x fewer bits per pixel, so the
# bigger video comes out visibly worse than the smaller one it replaced.
# CRF is per-pixel by construction, so it does not scale.
_REF_PIXELS = 720 * 1280
_QUALITY = {
    "high":     (20, 4500, 6500, 9000),
    "balanced": (23, 2500, 3800, 6000),
    "small":    (26, 1400, 2200, 4000),
}


def _rates(settings: Settings) -> tuple[int, str, str, str]:
    """(crf, bitrate, maxrate, bufsize) for this preset at this frame size."""
    crf, avg, peak, buf = _QUALITY.get(settings.quality, _QUALITY["balanced"])
    factor = max(0.25, (settings.width * settings.height) / _REF_PIXELS)
    return (crf, f"{round(avg * factor)}k", f"{round(peak * factor)}k",
            f"{round(buf * factor)}k")


def target_bitrate(settings: Settings) -> str:
    return _rates(settings)[1]


def estimated_size_mb(settings: Settings, seconds: float | None = None) -> float:
    """Rough finished size, for showing the user before a render."""
    seconds = settings.target_duration if seconds is None else seconds
    video_kbps = float(target_bitrate(settings).rstrip("k"))
    audio_kbps = float(str(settings.audio_bitrate).rstrip("k") or 128)
    return (video_kbps + audio_kbps) * seconds / 8 / 1024


def video_encode_args(settings: Settings, encoder: str) -> list[str]:
    crf, bitrate, maxrate, bufsize = _rates(settings)
    gop = str(settings.fps * 2)
    args = ["-c:v", encoder]

    if encoder == "h264_nvenc":
        # No -cq here: setting it switches nvenc to target-quality mode and
        # the bitrate caps stop being honoured.
        args += ["-preset", "p5", "-tune", "hq", "-rc", "vbr",
                 "-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize,
                 "-profile:v", "high", "-b_ref_mode", "0"]
    elif encoder == "h264_qsv":
        args += ["-preset", "medium",
                 "-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize,
                 "-profile:v", "high"]
    elif encoder == "h264_amf":
        args += ["-quality", "balanced", "-rc", "vbr_peak",
                 "-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize,
                 "-profile:v", "high"]
    else:
        args += ["-preset", "veryfast", "-crf", str(crf),
                 "-maxrate", maxrate, "-bufsize", bufsize,
                 "-profile:v", "high", "-level", "4.0"]

    # Fixed GOP + CFR keeps every segment byte-compatible for concat -c copy.
    args += ["-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
             "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(settings.fps)]
    return args


def audio_encode_args(settings: Settings) -> list[str]:
    return ["-c:a", "aac", "-b:a", settings.audio_bitrate,
            "-ar", str(settings.audio_rate), "-ac", "2"]
