"""Output validation (req 8): prove the file we just wrote is what was asked for.

Checked against the settings actually used, not hardcoded numbers, so changing
the target format keeps validation honest.
"""

from __future__ import annotations

from pathlib import Path

from . import ffmpeg
from .config import Settings
from .models import RenderPlan, ValidationReport

# A 3-minute 720x1280 H.264 clip outside this range means something went wrong
# with the bitrate settings.
_MIN_MB_PER_MIN = 0.4
_MAX_MB_PER_MIN = 60.0


def validate_output(path: Path | str, settings: Settings,
                    plan: RenderPlan | None = None) -> ValidationReport:
    path = Path(path)
    if not path.exists():
        return ValidationReport(ok=False, path=path,
                                problems=["Output file was not created."])

    try:
        info = ffmpeg.probe(path)
    except ffmpeg.FFmpegError as exc:
        return ValidationReport(ok=False, path=path,
                                problems=[f"Output is unreadable: {exc}"])

    problems: list[str] = []

    if (info.width, info.height) != (settings.width, settings.height):
        problems.append(
            f"Resolution is {info.width}x{info.height}, expected "
            f"{settings.width}x{settings.height}."
        )
    # the configured ratio within a hair, guarding against a stray SAR.
    # Not a hardcoded 9/16: the frame is user-selectable (9:16, 16:9, 1:1).
    wanted = settings.width / settings.height if settings.height else 9 / 16
    if info.height and abs(info.width / info.height - wanted) > 0.01:
        problems.append(
            f"Aspect ratio is {info.width}:{info.height}, not "
            f"{settings.width}:{settings.height}."
        )
    if info.vcodec.lower() not in ("h264", "avc1"):
        problems.append(f"Video codec is {info.vcodec}, expected H.264.")
    if not info.has_audio:
        problems.append("Output has no audio track.")
    if path.suffix.lower() != ".mp4":
        problems.append(f"Container is {path.suffix}, expected .mp4.")

    # Two separate questions. First: did the encoder produce what was planned?
    expected = plan.total_duration if plan else settings.target_duration
    # Allow the configured tolerance, and never less than a frame or two.
    slack = max(settings.tolerance, 0.5) if not settings.exact_duration else 0.75
    if abs(info.duration - expected) > slack:
        problems.append(
            f"Duration is {info.duration:.2f}s, expected {expected:.2f}s "
            f"(off by {info.duration - expected:+.2f}s)."
        )

    # Second, and the one that matters to whoever posts the video: is it the
    # length that was actually asked for? A plan that ran out of material is
    # internally consistent but still not a deliverable, so checking only
    # against the plan would wave a 3-second file through as fine.
    target = float(settings.target_duration)
    if abs(info.duration - target) > max(settings.tolerance, 1.0):
        shortfall = target - info.duration
        if shortfall > 0:
            problems.append(
                f"Output is only {info.duration:.1f}s, not the {target:g}s "
                f"asked for - the input did not contain enough usable clips "
                f"({shortfall:.0f}s short)."
            )
        else:
            problems.append(
                f"Output is {info.duration:.1f}s, longer than the {target:g}s "
                f"asked for by {-shortfall:.0f}s."
            )

    if info.fps and abs(info.fps - settings.fps) > 1.0:
        problems.append(f"Frame rate is {info.fps:.2f}, expected {settings.fps}.")

    size_mb = info.size_bytes / (1024 * 1024)
    minutes = max(info.duration / 60.0, 0.01)
    per_min = size_mb / minutes
    if per_min < _MIN_MB_PER_MIN:
        problems.append(f"File looks too small ({size_mb:.1f} MB) to be valid.")
    elif per_min > _MAX_MB_PER_MIN:
        problems.append(
            f"File is unusually large ({size_mb:.1f} MB); check the quality "
            "preset."
        )

    return ValidationReport(
        ok=not problems,
        path=path,
        duration=info.duration,
        width=info.width,
        height=info.height,
        fps=info.fps,
        has_audio=info.has_audio,
        size_bytes=info.size_bytes,
        problems=problems,
    )


def describe(report: ValidationReport) -> str:
    head = (f"{report.path.name}: {report.width}x{report.height}, "
            f"{report.duration:.2f}s, {report.size_mb:.1f} MB, "
            f"{report.fps:.0f} fps, audio: {'yes' if report.has_audio else 'no'}")
    if report.ok:
        return head + "  [PASS]"
    return "\n".join([head + "  [FAIL]"] + [f"  - {p}" for p in report.problems])
