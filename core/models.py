from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class MediaInfo:
    """What ffprobe tells us about a file."""

    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    vcodec: str
    size_bytes: int
    # Duration of the video track alone. The container duration is the longest
    # track, which for short clips is often the audio (AAC rounds up to its
    # own frame size), so it overstates how many video frames actually exist.
    video_duration: float = 0.0
    frame_count: int = 0

    @property
    def is_portrait(self) -> bool:
        return self.height >= self.width

    @property
    def usable_duration(self) -> float:
        """Duration we can actually cut video from."""
        if self.frame_count > 0 and self.fps > 0:
            return self.frame_count / self.fps
        return self.video_duration or self.duration


@dataclass(frozen=True)
class Clip:
    """A time range inside one source file."""

    source: Path
    start: float
    end: float
    index: int = 0
    # < 1.0 marks a boundary the detector was unsure about -> Vision LLM
    # candidate (req 11). The engine never blocks on it.
    confidence: float = 1.0
    # True when this clip's START boundary was placed deliberately to break up
    # a stretch longer than the whole output - not because a cut was detected
    # there. The vision check must not review these: it would correctly say
    # "one continuous shot" and merge away the only thing making the video
    # usable.
    forced: bool = False

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Segment:
    """One piece of the output timeline."""

    clip: Clip
    kind: str  # "original" | "reaction"
    label: str = ""
    # Set only on the tail segment when we shave it to land on exactly 3:00.
    render_duration: Optional[float] = None
    # Pre-normalised file from the reaction cache, if we have one.
    cached_path: Optional[Path] = None

    @property
    def out_duration(self) -> float:
        if self.render_duration is not None:
            return self.render_duration
        return self.clip.duration

    @property
    def is_trimmed(self) -> bool:
        return self.render_duration is not None


@dataclass
class RenderPlan:
    """The chosen timeline, before any encoding happens."""

    segments: list[Segment] = field(default_factory=list)
    target: float = 180.0
    # Duration reached using whole clips only, before any tail trim.
    natural_duration: float = 0.0
    originals_available: int = 0
    originals_used: int = 0
    reactions_used: list[str] = field(default_factory=list)
    rotation_offset: int = 0
    exhausted: bool = False  # ran out of material before hitting target
    # False when whole clips could not be made to land on the target
    exact_hit: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return sum(s.out_duration for s in self.segments)

    @property
    def drift(self) -> float:
        return self.total_duration - self.target


@dataclass
class ValidationReport:
    ok: bool
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    size_bytes: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


@dataclass
class JobResult:
    ok: bool
    source: Path
    output: Optional[Path] = None
    plan: Optional[RenderPlan] = None
    validation: Optional[ValidationReport] = None
    elapsed: float = 0.0
    error: str = ""


@dataclass(frozen=True)
class ReactionAsset:
    """One saved reaction video from the persistent library (req 12)."""

    id: int
    path: Path
    label: str
    duration: float
    width: int = 0
    height: int = 0
    has_audio: bool = True
    active: bool = True
    # Pre-normalised 720x1280 copy, reused across jobs so a reaction is only
    # ever encoded once per render-settings change.
    cached_path: Optional[Path] = None
    cache_signature: str = ""
    added_at: str = ""

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def as_clip(self) -> "Clip":
        return Clip(source=self.path, start=0.0, end=self.duration,
                    index=self.id, confidence=1.0)
