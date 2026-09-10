"""Encoding: turn a RenderPlan into one 9:16 MP4 at the configured frame (req 8).

Every segment is normalised to byte-compatible encode settings first, then the
segments are stitched with the concat demuxer using stream copy -- no second
generation loss and the stitch itself costs almost nothing.

Reactions are normalised into a persistent cache. Across a 50-60 video day
(req 10) each reaction is therefore encoded once, not once per video, which is
where most of the wall-clock saving comes from.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

from . import ffmpeg
from .config import Settings, TEMP_DIR, ensure_dirs
from .models import Clip, MediaInfo, ReactionAsset, RenderPlan, Segment

StatusCb = Optional[Callable[[str], None]]
ProgressCb = Optional[Callable[[float], None]]  # 0.0 .. 1.0
CancelCb = Optional[Callable[[], bool]]


class RenderError(RuntimeError):
    pass


# Fixed timescale so every segment carries an identical video track header,
# which is what lets the concat demuxer stream-copy them together.
_TIMESCALE = "30000"


def _noop_status(_msg: str) -> None:
    pass


class Renderer:
    def __init__(self, settings: Settings, library=None):
        self.settings = settings
        self.library = library
        self.encoder = ffmpeg.pick_encoder(settings)
        self._probe_cache: dict[Path, MediaInfo] = {}

    # ------------------------------------------------------------------
    def probe(self, path: Path) -> MediaInfo:
        path = Path(path)
        if path not in self._probe_cache:
            self._probe_cache[path] = ffmpeg.probe(path)
        return self._probe_cache[path]

    # ------------------------------------------------------------------
    def warm_reaction_cache(self, reactions: list[ReactionAsset],
                            on_status: StatusCb = None,
                            on_progress: ProgressCb = None,
                            cancelled: CancelCb = None) -> dict[int, Path]:
        """Normalise every reaction once and remember where it landed.

        Returns {reaction_id: normalised_path}. Safe to call repeatedly: a
        reaction whose cache matches the current render settings is skipped.
        """
        status = on_status or _noop_status
        signature = self.settings.render_signature()
        ensure_dirs()

        result: dict[int, Path] = {}
        todo: list[ReactionAsset] = []
        for r in reactions:
            hit = (self.library.valid_cache(r, signature)
                   if self.library else None)
            if hit:
                result[r.id] = hit
            else:
                todo.append(r)

        if not todo:
            if on_progress:
                on_progress(1.0)
            return result

        total = sum(r.duration for r in todo) or 1.0
        done = 0.0
        for i, r in enumerate(todo, 1):
            status(f"Preparing reaction {i}/{len(todo)}: {r.label}")
            target = (self.library.cache_target(r, signature) if self.library
                      else TEMP_DIR / f"reaction_{r.id}_{signature}.mp4")
            base = done

            def _progress(secs: float, _base=base) -> None:
                if on_progress:
                    on_progress(min(1.0, (_base + secs) / total))

            self._normalise(
                Clip(source=r.path, start=0.0, end=r.duration, index=r.id),
                target, on_progress=_progress, cancelled=cancelled,
                label=f"reaction {r.label}",
            )
            if self.library:
                self.library.set_cache(r.id, target, signature)
            result[r.id] = target
            done += r.duration

        if on_progress:
            on_progress(1.0)
        return result

    # ------------------------------------------------------------------
    def render(self, plan: RenderPlan, output: Path,
               reaction_cache: Optional[dict[int, Path]] = None,
               on_status: StatusCb = None,
               on_progress: ProgressCb = None,
               cancelled: CancelCb = None) -> Path:
        """Encode `plan` to `output`. Returns the written path."""
        status = on_status or _noop_status
        ensure_dirs()
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        job_dir = Path(tempfile.mkdtemp(prefix="job_", dir=str(TEMP_DIR)))
        try:
            parts = self._prepare_parts(plan, job_dir, reaction_cache or {},
                                        status, on_progress, cancelled)
            status("Joining segments")
            self._concat(parts, output, job_dir, on_progress, cancelled)
            return output
        finally:
            if not self.settings.keep_temp:
                shutil.rmtree(job_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    def _prepare_parts(self, plan: RenderPlan, job_dir: Path,
                       reaction_cache: dict[int, Path], status: Callable,
                       on_progress: ProgressCb,
                       cancelled: CancelCb) -> list[Path]:
        """Normalise (or reuse) one file per timeline segment, in order."""
        # Only segments that actually need encoding count toward progress.
        pending = [s for s in plan.segments
                   if self._reuse_path(s, reaction_cache) is None]
        total_work = sum(s.out_duration for s in pending) or 1.0
        done = 0.0
        parts: list[Path] = []
        encoded = 0

        for i, seg in enumerate(plan.segments):
            if cancelled and cancelled():
                raise ffmpeg.Cancelled("render cancelled")

            reuse = self._reuse_path(seg, reaction_cache)
            if reuse is not None:
                parts.append(reuse)
                continue

            encoded += 1
            status(f"Encoding segment {i + 1}/{len(plan.segments)} "
                   f"({seg.kind}: {seg.label})")
            target = job_dir / f"seg_{i:04d}.mp4"
            base = done

            def _progress(secs: float, _base=base) -> None:
                if on_progress:
                    # Segment encoding is the bulk of the job; leave the last
                    # slice of the bar for the concat step.
                    on_progress(min(0.97, 0.97 * (_base + secs) / total_work))

            self._normalise(seg.clip, target,
                            duration_override=seg.out_duration,
                            on_progress=_progress, cancelled=cancelled,
                            label=f"segment {i + 1}")
            parts.append(target)
            done += seg.out_duration

        if not parts:
            raise RenderError("The plan produced no segments to encode.")
        return parts

    def _reuse_path(self, seg: Segment, reaction_cache: dict[int, Path]) -> Optional[Path]:
        """A pre-normalised file for this segment, if one is usable."""
        if seg.kind != "reaction" or seg.is_trimmed:
            return None  # originals and the shaved tail always get encoded
        for candidate in (reaction_cache.get(seg.clip.index), seg.cached_path):
            if candidate and Path(candidate).exists():
                return Path(candidate)
        return None

    # ------------------------------------------------------------------
    def _normalise(self, clip: Clip, target: Path,
                   duration_override: Optional[float] = None,
                   on_progress: ProgressCb = None,
                   cancelled: CancelCb = None,
                   label: str = "segment") -> Path:
        """Re-encode one clip into the fixed output format."""
        s = self.settings
        duration = duration_override if duration_override is not None else clip.duration
        if duration <= 0:
            raise RenderError(f"{label}: zero-length clip.")

        info = self.probe(clip.source)
        target.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
            "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            "-ss", f"{clip.start:.3f}", "-i", str(clip.source),
        ]
        if not info.has_audio:
            # Silent track, so every segment has the same stream layout and
            # the concat stream-copy stays valid.
            cmd += ["-f", "lavfi", "-i",
                    f"anullsrc=channel_layout=stereo:sample_rate={s.audio_rate}"]
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a:0"]

        # Pin the segment to a whole number of frames and trim/pad the audio to
        # match. Without this, video truncates down to a frame boundary while
        # AAC rounds up, so each segment drifts by up to a frame in either
        # direction and a 20-segment timeline lands a quarter-second short.
        frames = max(1, int(round(duration * s.fps)))
        exact = frames / float(s.fps)

        # Read slightly past the target so the frame count is always reachable.
        cmd += ["-t", f"{exact + 2.0 / s.fps:.4f}"]
        cmd += ["-vf", ffmpeg.video_filter(s)]
        cmd += ["-af", ("aformat=sample_fmts=fltp:channel_layouts=stereo,"
                        f"aresample={s.audio_rate}:async=1:first_pts=0,"
                        f"atrim=end={exact:.4f},"
                        f"apad=whole_dur={exact:.4f}")]
        cmd += ffmpeg.video_encode_args(s, self.encoder)
        cmd += ffmpeg.audio_encode_args(s)
        cmd += ["-frames:v", str(frames)]
        cmd += ["-video_track_timescale", _TIMESCALE,
                "-map_metadata", "-1", "-map_chapters", "-1",
                str(target)]

        try:
            ffmpeg.run(cmd, on_progress=on_progress, cancelled=cancelled,
                       label=label)
        except ffmpeg.FFmpegError as exc:
            if self.encoder != "libx264":
                # Hardware encoders can refuse odd inputs; software always works.
                fallback = list(cmd)
                _swap_encoder_args(fallback, s, self.encoder, "libx264")
                ffmpeg.run(fallback, on_progress=on_progress,
                           cancelled=cancelled, label=f"{label} (libx264)")
            else:
                raise RenderError(f"{label} failed: {exc}\n{exc.log}") from exc

        if not target.exists() or target.stat().st_size == 0:
            raise RenderError(f"{label} produced no output file.")
        return target

    # ------------------------------------------------------------------
    def _concat(self, parts: list[Path], output: Path, job_dir: Path,
                on_progress: ProgressCb = None,
                cancelled: CancelCb = None) -> None:
        list_file = job_dir / "concat.txt"
        lines = []
        for p in parts:
            safe = Path(p).resolve().as_posix().replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        def _progress(_secs: float) -> None:
            if on_progress:
                on_progress(0.99)

        base = [
            ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
            "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
        ]
        copy_cmd = base + ["-c", "copy", "-movflags", "+faststart",
                           "-fflags", "+genpts", str(output)]
        try:
            ffmpeg.run(copy_cmd, on_progress=_progress, cancelled=cancelled,
                       label="concat")
        except ffmpeg.Cancelled:
            raise
        except ffmpeg.FFmpegError:
            # Stream copy is the fast path; if the segments turn out not to be
            # bit-compatible, re-encode the join rather than fail the job.
            s = self.settings
            recode = base + ffmpeg.video_encode_args(s, self.encoder)
            recode += ffmpeg.audio_encode_args(s)
            recode += ["-movflags", "+faststart", str(output)]
            ffmpeg.run(recode, on_progress=_progress, cancelled=cancelled,
                       label="concat (re-encode)")

        if not output.exists() or output.stat().st_size == 0:
            raise RenderError("Concat produced no output file.")
        if on_progress:
            on_progress(1.0)


def _swap_encoder_args(cmd: list[str], settings: Settings, old: str,
                       new: str) -> None:
    """Replace the encoder-specific argument run in `cmd`, in place."""
    old_args = ffmpeg.video_encode_args(settings, old)
    new_args = ffmpeg.video_encode_args(settings, new)
    for i in range(len(cmd) - len(old_args) + 1):
        if cmd[i:i + len(old_args)] == old_args:
            cmd[i:i + len(old_args)] = new_args
            return
    # Fall back to swapping just the codec name.
    for i, token in enumerate(cmd):
        if token == old:
            cmd[i] = new
            return
