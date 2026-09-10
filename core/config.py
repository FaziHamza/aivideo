from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# Frozen (.exe) and source runs disagree about what "the project" is. Under
# PyInstaller __file__ points inside the unpacked bundle, which is temporary in
# onefile mode and read-only in spirit either way -- so data/ has to hang off
# the executable's own folder instead, or the library and settings vanish
# between runs.
FROZEN = getattr(sys, "frozen", False)
PROJECT_ROOT = (Path(sys.executable).resolve().parent if FROZEN
                else Path(__file__).resolve().parent.parent)
# Where PyInstaller unpacked the read-only payload (bundled ffmpeg lives here).
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"      # normalised reaction segments (reused daily)
TEMP_DIR = DATA_DIR / "temp"        # per-job scratch, wiped after each render
OUTPUT_DIR = DATA_DIR / "output"
DB_PATH = DATA_DIR / "library.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

# Quality presets map to whichever encoder we end up using.
QUALITY_PRESETS = ("high", "balanced", "small")

# Bumped whenever the encode recipe itself changes (filters, rate control,
# frame alignment). Cached reaction segments built by an older recipe are then
# rebuilt instead of being silently mixed with new ones.
RECIPE_VERSION = 2


@dataclass
class Settings:
    # --- timeline (req 7) ---
    target_duration: float = 180.0
    tolerance: float = 2.0
    # True  -> shave the final segment so the file is exactly 3:00
    # False -> leave whole clips, land within `tolerance`
    exact_duration: bool = True

    # --- output format (req 8) ---
    # 1080x1920 is what Shorts/Reels/TikTok actually serve. 720x1280 was the
    # first default and read as "video size chota kar deta" the moment the
    # output sat next to the 1080p input it was cut from.
    width: int = 1080
    height: int = 1920
    fps: int = 30
    # How non-9:16 material is fitted: blur | pad | crop
    fit_mode: str = "blur"
    quality: str = "balanced"
    audio_bitrate: str = "128k"
    audio_rate: int = 48000
    encoder: str = "auto"  # auto | h264_nvenc | h264_qsv | h264_amf | libx264

    # --- scene detection (req 3) ---
    # content/adaptive use PySceneDetect; ffmpeg uses FFmpeg's own scene
    # filter and needs no extra packages.
    #
    # histogram (default) compares which colours are present either side of a
    # candidate cut, so camera motion does not read as a cut. ffmpeg, content
    # and adaptive all measure adjacent-frame difference instead, which splits
    # one real clip into several on fast motion and then gives each piece its
    # own reaction. Costs about the same: one decode pass either way.
    detector: str = "histogram"  # histogram | ffmpeg | content | adaptive
    # How much the two sides of a cut must differ, 0..1, measured over a 4x4
    # grid of colour histograms.
    #
    # 0 means work it out per video from that video's own score spread, which
    # is the default because a number calibrated on one video says nothing
    # about the next one and this tool gets a different video every time.
    # Across six test videos the automatic value came out at 0.45-0.50 -
    # the same place hand-calibration landed, but derived rather than assumed.
    #
    # Set a value above 0 to override it: higher splits less, lower splits more.
    cut_strength: float = 0.0
    scene_threshold: float = 27.0
    # 0..1 scene score for the FFmpeg detector (higher = fewer cuts).
    ffmpeg_scene_threshold: float = 0.20
    min_scene_len: float = 1.0
    # Clips shorter than this are merged into a neighbour rather than shown.
    # A boundary that turned out to be mid-shot usually leaves a sliver, so
    # merging slivers away removes most spurious boundaries without having to
    # judge the content - see _merge_short_clips.
    #
    # 0 means work it out per video, as a fraction of that video's median clip
    # length. A fixed value cannot be right for both footage cut every two
    # seconds and footage that runs ten seconds a shot. Negative disables
    # merging entirely.
    min_clip_duration: float = 0.0
    # Clips longer than this are split at their most cut-like inner point.
    # Off by default: an arbitrary split puts a reaction in the middle of one
    # continuous shot, which is the same complaint as a missed cut wearing a
    # different hat. Set it only if a genuinely long single shot is a problem.
    max_clip_duration: float = 0.0
    downscale: int = 0  # 0 = let PySceneDetect pick

    # best       = rate every clip and use the highest-rated ones that fit,
    #              still in chronological order. Needs `rank_clips`; falls back
    #              to sequential when rating is unavailable.
    # sequential = walk the clips in order, one reaction each, until the target
    #              is full.
    # fit        = pick whichever clips add up closest to the target, which
    #              nails the length but skips around the video.
    clip_selection: str = "best"  # sequential | fit

    # --- reactions (req 4, 5, 12) ---
    # Rotation position carried across jobs so batched videos don't all open
    # with the same reaction.
    rotation_offset: int = 0
    rotation_advances_per_job: bool = True

    # --- batch (req 9) ---
    output_dir: str = str(OUTPUT_DIR)
    output_template: str = "{stem}_reaction_{n:03d}.mp4"
    keep_temp: bool = False

    # --- model-backed judgement (XtroEdge AI) ---
    #
    # Two separate jobs, with different justifications:
    #
    #   rank_clips      Which clips are worth reacting to. This is the one job
    #                   here that a model is genuinely needed for - no pixel
    #                   statistic judges "is this funny" - so it is on by
    #                   default. Costs about 2 requests per video.
    #   vision_enabled  Whether a boundary is a real cut. Also needed, and for
    #                   the same reason: with every boundary of the labelled
    #                   video checked, the pixel measures alone split 7 of 24
    #                   continuous shots down the middle. With this on, the
    #                   detector runs trigger-happy (16/17 cuts proposed) and
    #                   the model cleans up: 12 of 17 cuts kept, 1-2 of 24
    #                   shots split. That is the trade this project wants -
    #                   a missed cut leaves two scenes in one clip, which
    #                   reads as ordinary, while a false split drops a
    #                   reaction mid-shot, which reads as broken. On by
    #                   default; costs about 3 requests per video.
    #
    # Both fail soft. No key, no budget, a network error, a reply that does not
    # parse: the pipeline carries on with the non-model answer and says so in
    # the job notes. Neither can fail a render.
    rank_clips: bool = True
    # Skip ranking when fewer than this many requests remain today. The key
    # allows 500 a day and a day is 50-60 videos, so leaving a reserve keeps a
    # long batch from stopping partway through for quota.
    rank_min_requests: int = 30
    vision_enabled: bool = True
    # Only boundaries below this confidence are sent for verification. It is
    # above 1.0 - meaning all of them - on purpose. Gating on confidence was
    # tried first and does not work: the cheap score put only 2 of 21
    # boundaries in the doubtful band while 7 were actually wrong, because real
    # cuts and fast camera moves score in the same range (see the vision module
    # docstring). Twenty boundaries batch into two requests, so checking
    # everything costs about what checking a third of it would have.
    vision_confidence_floor: float = 1.01
    # Cap on boundaries sent for verification per video, least confident first.
    # Three batches; a 3-minute input yields about 20 boundaries.
    vision_max_checks: int = 48
    # Branded model name for the XtroEdge gateway. v3 is the only tier with
    # vision, so image work must use it.
    vision_model: str = "XtroEdge Pro v3"

    _detected_encoder: str = field(default="", repr=False)

    # ---------- persistence ----------
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or SETTINGS_PATH
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path | None = None) -> None:
        path = path or SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    # ---------- derived ----------
    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def render_signature(self) -> str:
        """Identifies the encode settings a cached segment was built with.

        Changing any of these invalidates the reaction cache.
        """
        parts = (RECIPE_VERSION, self.width, self.height, self.fps,
                 self.fit_mode, self.quality, self.audio_bitrate,
                 self.audio_rate)
        return "-".join(str(p) for p in parts)


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, TEMP_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
