"""Command-line front end for the reaction-video engine.

The desktop app and this CLI are two thin shells over the same `core` package,
so anything provable here is provable in the GUI.

    python cli.py doctor
    python cli.py reactions add D:/reactions/*.mp4
    python cli.py plan D:/input/long.mp4
    python cli.py render D:/input/long.mp4 --open
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import __version__, ffmpeg, planner, validate
from core.config import Settings, ensure_dirs
from core.library import VIDEO_SUFFIXES, ReactionLibrary
from core.pipeline import BatchProgress, Pipeline, summarise

_BAR_WIDTH = 34


def _bar(fraction: float, phase: str = "") -> str:
    filled = int(_BAR_WIDTH * max(0.0, min(1.0, fraction)))
    return (f"[{'#' * filled}{'.' * (_BAR_WIDTH - filled)}] "
            f"{fraction * 100:5.1f}%  {phase:<8}")


def _expand(patterns: list[str]) -> list[Path]:
    """Accept files, directories and glob patterns."""
    out: list[Path] = []
    for raw in patterns:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(f for f in p.iterdir()
                              if f.suffix.lower() in VIDEO_SUFFIXES))
        elif any(ch in raw for ch in "*?["):
            parent = p.parent if str(p.parent) else Path(".")
            out.extend(sorted(parent.glob(p.name)))
        elif p.exists():
            out.append(p)
        else:
            print(f"  ! not found: {raw}", file=sys.stderr)
    # de-duplicate, keep order
    seen: set[Path] = set()
    unique = []
    for f in out:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            unique.append(f)
    return unique


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    ensure_dirs()
    settings = Settings.load()
    print(f"Reaction Video Builder {__version__}\n")
    print("Tools")
    for name, version in ffmpeg.tool_versions().items():
        print(f"  {name:9} {version}")

    print("\nEncoders")
    try:
        print(f"  available  {', '.join(ffmpeg.available_encoders())}")
        print(f"  selected   {ffmpeg.pick_encoder(settings)}")
    except ffmpeg.FFmpegMissing as exc:
        print(f"  !! {exc}")

    print("\nPython packages")
    for module, label in (("scenedetect", "PySceneDetect"), ("cv2", "OpenCV"),
                          ("PySide6", "PySide6")):
        try:
            mod = __import__(module)
            print(f"  {label:14} {getattr(mod, '__version__', 'installed')}")
        except ImportError:
            print(f"  {label:14} MISSING")

    library = ReactionLibrary()
    print(f"\nReaction library ({library.db_path})")
    print(f"  total {library.count()}, usable {len(library.active_reactions())}")
    missing = library.missing_files()
    if missing:
        print(f"  !! {len(missing)} entries point at files that are gone:")
        for r in missing[:5]:
            print(f"     {r.path}")

    print(f"\nOutput format  {settings.resolution} @ {settings.fps}fps, "
          f"{settings.target_duration:g}s target, fit={settings.fit_mode}, "
          f"quality={settings.quality}")
    print(f"Output folder  {settings.output_dir}")
    print(f"Rendered today {library.jobs_done_today()}")

    problems = Pipeline(settings, library).preflight()
    print()
    if problems:
        print("NOT READY:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("READY")
    return 0


def cmd_reactions(args) -> int:
    library = ReactionLibrary()
    action = args.action

    if action == "list":
        rows = library.all_reactions()
        if not rows:
            print("Library is empty.")
            return 0
        print(f"{'ID':>4}  {'ON':2}  {'DURATION':>8}  {'SIZE':>10}  LABEL")
        for r in rows:
            flag = "y" if r.active else "-"
            missing = "" if r.exists else "  [FILE MISSING]"
            cached = "  [cached]" if r.cached_path and r.cached_path.exists() else ""
            print(f"{r.id:>4}  {flag:2}  {r.duration:>7.2f}s  "
                  f"{r.width:>4}x{r.height:<4}  {r.label}{cached}{missing}")
        total = sum(r.duration for r in rows if r.active)
        print(f"\n{len(rows)} reactions, {total:.1f}s of active material.")
        return 0

    if action in ("add", "replace"):
        files = _expand(args.paths)
        if not files:
            print("No video files matched.", file=sys.stderr)
            return 1
        if action == "replace":
            added, errors = library.replace_all(files)
            print(f"Library replaced with {len(added)} reaction(s).")
        else:
            added, errors = library.add_many(files)
            print(f"Added/updated {len(added)} reaction(s).")
        for r in added:
            print(f"  {r.id:>4}  {r.duration:6.2f}s  {r.label}")
        for e in errors:
            print(f"  ! {e}", file=sys.stderr)
        return 0 if added else 1

    if action == "remove":
        for rid in args.ids:
            library.remove(rid)
            print(f"Removed reaction {rid}.")
        return 0

    if action == "clear":
        count = library.count()
        library.clear()
        print(f"Cleared {count} reaction(s).")
        return 0

    if action in ("enable", "disable"):
        for rid in args.ids:
            library.set_active(rid, action == "enable")
            print(f"{action.title()}d reaction {rid}.")
        return 0

    if action == "purge-cache":
        removed = library.purge_cache(Settings.load().render_signature())
        print(f"Removed {removed} stale cached segment(s).")
        return 0

    print(f"Unknown action: {action}", file=sys.stderr)
    return 2


def cmd_plan(args) -> int:
    settings = _settings_from_args(args)
    pipe = Pipeline(settings)
    problems = pipe.preflight()
    if problems:
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    for source in _expand(args.inputs):
        print(f"\n=== {source.name} ===")
        try:
            plan, clips = pipe.plan_only(
                source,
                on_progress=lambda f, ph: print(f"\r  {_bar(f, ph)}", end=""),
                on_status=lambda m: print(f"\r  {m:<60}"),
            )
        except Exception as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            continue
        print("\r" + " " * 70)
        print(planner.describe_plan(plan))
        if args.verbose:
            print("\n  #  KIND      LABEL                 START      DUR")
            for i, seg in enumerate(plan.segments, 1):
                mark = " (trimmed)" if seg.is_trimmed else ""
                print(f"  {i:>2}  {seg.kind:<9} {seg.label:<20} "
                      f"{seg.clip.start:8.2f}  {seg.out_duration:6.2f}{mark}")
    return 0


def cmd_render(args) -> int:
    settings = _settings_from_args(args)
    if args.output_dir:
        settings.output_dir = str(Path(args.output_dir).resolve())
    sources = _expand(args.inputs)
    if not sources:
        print("No input videos matched.", file=sys.stderr)
        return 1

    pipe = Pipeline(settings)
    problems = pipe.preflight()
    if problems:
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"Rendering {len(sources)} video(s) with encoder "
          f"{pipe.renderer.encoder}, one at a time.\n")

    def _progress(bp: BatchProgress) -> None:
        head = f"[{bp.index + 1}/{bp.total}] {bp.source.name[:28]:<28}"
        print(f"\r{head} {_bar(bp.fraction, bp.phase)}", end="", flush=True)

    def _done(index: int, result) -> None:
        print("\r" + " " * 100, end="\r")
        if result.ok and result.validation:
            print(f"[{index + 1}] OK   {validate.describe(result.validation)}"
                  f"  ({result.elapsed:.1f}s)")
        elif result.validation:
            print(f"[{index + 1}] WARN {validate.describe(result.validation)}")
        else:
            print(f"[{index + 1}] FAIL {result.source.name}: {result.error}")
        if result.plan:
            for note in result.plan.notes:
                print(f"         note: {note}")

    results = pipe.process_batch(sources, on_batch_progress=_progress,
                                 on_job_done=_done)
    print()
    print(summarise(results))

    if args.open and results and results[0].output:
        import os
        os.startfile(Path(results[0].output).parent)  # noqa: S606
    return 0 if all(r.ok for r in results) else 1


def cmd_boundaries(args) -> int:
    """Write a before/after image for each detected boundary, for labelling.

    Judging a boundary needs a full-size frame half a second either side. At
    smaller sizes or a shorter gap a camera mid-move looks like a different
    scene - three boundaries were mislabelled that way while calibrating the
    detector, which is why this writes big frames with a wide gap.
    """
    import json
    import subprocess

    from core import scenes
    from core.config import DATA_DIR

    settings = _settings_from_args(args)
    sources = _expand(args.inputs)
    if not sources:
        print("No input videos matched.", file=sys.stderr)
        return 1

    labels_path = Path(__file__).resolve().parent / "tests" / "labels.json"
    known: list[float] = []
    if args.unlabelled_only and labels_path.exists():
        try:
            labels = json.loads(labels_path.read_text(encoding="utf-8"))
            known = ([e["at"] for e in labels.get("cuts", [])]
                     + [e["at"] for e in labels.get("same_scene", [])])
        except (OSError, json.JSONDecodeError, KeyError):
            known = []

    for source in sources:
        out_dir = DATA_DIR / "labelling" / source.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        for stale in out_dir.glob("*.jpg"):
            stale.unlink()

        print(f"\n=== {source.name} ===")
        clips, _ = scenes.detect_clips(source, settings)
        boundaries = [c.start for c in clips[1:]]
        if args.unlabelled_only and known:
            boundaries = [b for b in boundaries
                          if not any(abs(b - k) <= 0.5 for k in known)]
            print(f"{len(clips) - 1} boundaries, "
                  f"{len(boundaries)} not yet labelled")
        else:
            print(f"{len(boundaries)} boundaries")

        index = []
        for number, at in enumerate(boundaries, 1):
            target = out_dir / f"{number:02d}_at_{at:07.2f}s.jpg"
            graph = (f"[0:v]scale={args.width}:-2[a];"
                     f"[1:v]scale={args.width}:-2[b];[a][b]hstack=inputs=2")
            subprocess.run([
                ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
                "-y",
                "-ss", f"{max(0.0, at - args.gap):.3f}", "-i", str(source),
                "-ss", f"{at + args.gap:.3f}", "-i", str(source),
                "-filter_complex", graph,
                "-frames:v", "1", "-q:v", "3", str(target),
            ], capture_output=True)
            if target.exists():
                index.append(f"{number:>3}  t={at:7.2f}s   {target.name}")

        (out_dir / "index.txt").write_text(
            "LEFT = half a second before the cut, "
            "RIGHT = half a second after.\n\n"
            "cut   the two halves show different footage - a different\n"
            "      place, subject, or camera setup\n"
            "same  one continuous shot, something merely moved - the\n"
            "      camera panned or shook, or the subject walked or turned\n\n"
            "When you cannot tell, answer 'same'.\n\n"
            + "\n".join(index) + "\n",
            encoding="utf-8")

        print(f"wrote {len(index)} image(s) to {out_dir}")
        print(f"read {out_dir / 'index.txt'} for the rule and the list")
        if args.open and index:
            import os
            os.startfile(out_dir)  # noqa: S606
    return 0


def cmd_settings(args) -> int:
    settings = Settings.load()
    if args.set:
        from dataclasses import fields
        known = {f.name: f.type for f in fields(Settings)
                 if not f.name.startswith("_")}
        for pair in args.set:
            if "=" not in pair:
                print(f"Expected key=value, got {pair}", file=sys.stderr)
                return 2
            key, _, value = pair.partition("=")
            key = key.strip()
            if key not in known:
                print(f"Unknown setting: {key}", file=sys.stderr)
                return 2
            current = getattr(settings, key)
            try:
                if isinstance(current, bool):
                    parsed = value.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int):
                    parsed = int(value)
                elif isinstance(current, float):
                    parsed = float(value)
                else:
                    parsed = value
            except ValueError:
                print(f"Bad value for {key}: {value}", file=sys.stderr)
                return 2
            setattr(settings, key, parsed)
            print(f"{key} = {parsed}")
        settings.save()
        return 0

    from dataclasses import asdict
    for key, value in asdict(settings).items():
        if not key.startswith("_"):
            print(f"{key:28} {value}")
    return 0


def _settings_from_args(args) -> Settings:
    settings = Settings.load()
    for attr in ("target_duration", "fit_mode", "quality", "scene_threshold",
                 "ffmpeg_scene_threshold", "detector", "min_clip_duration",
                 "encoder"):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(settings, attr, value)
    if getattr(args, "whole_clips", False):
        settings.exact_duration = False
    return settings


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Reaction-video builder: long video in, 3-minute 9:16 MP4 out.",
    )
    # Before any subcommand, so `cli.py --version` answers without needing to
    # know one - which is what someone reading a bug report will reach for.
    parser.add_argument("--version", action="version",
                        version=f"Reaction Video Builder {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_render_options(p):
        p.add_argument("--target-duration", type=float,
                       help="output length in seconds (default 180)")
        p.add_argument("--fit-mode", choices=("blur", "pad", "crop"),
                       help="how non-9:16 sources are fitted")
        p.add_argument("--quality", choices=("high", "balanced", "small"))
        p.add_argument("--scene-threshold", type=float,
                       help="PySceneDetect threshold, lower finds more cuts "
                            "(default 27)")
        p.add_argument("--ffmpeg-scene-threshold", type=float,
                       help="FFmpeg detector threshold 0..1, lower finds more "
                            "cuts (default 0.20)")
        p.add_argument("--detector", choices=("content", "adaptive", "ffmpeg"),
                       help="boundary detector to use")
        p.add_argument("--min-clip-duration", type=float,
                       help="drop detected clips shorter than this")
        p.add_argument("--encoder", help="force an encoder, e.g. libx264")
        p.add_argument("--whole-clips", action="store_true",
                       help="never shave the tail; accept a small drift")

    p_doctor = sub.add_parser("doctor", help="check tools, packages and library")
    p_doctor.set_defaults(func=cmd_doctor)

    p_react = sub.add_parser("reactions", help="manage the reaction library")
    p_react.add_argument("action",
                         choices=("list", "add", "replace", "remove", "clear",
                                  "enable", "disable", "purge-cache"))
    p_react.add_argument("paths", nargs="*", help="files, folders or globs")
    p_react.add_argument("--ids", nargs="*", type=int, default=[],
                         help="reaction ids for remove/enable/disable")
    p_react.set_defaults(func=cmd_reactions)

    p_plan = sub.add_parser("plan", help="detect and plan without encoding")
    p_plan.add_argument("inputs", nargs="+")
    p_plan.add_argument("-v", "--verbose", action="store_true",
                        help="print the full timeline")
    add_render_options(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_render = sub.add_parser("render", help="render one or more videos")
    p_render.add_argument("inputs", nargs="+", help="files, folders or globs")
    p_render.add_argument("-o", "--output-dir")
    p_render.add_argument("--open", action="store_true",
                          help="open the output folder when finished")
    add_render_options(p_render)
    p_render.set_defaults(func=cmd_render)

    p_bounds = sub.add_parser(
        "boundaries",
        help="write before/after images for each detected cut, for labelling")
    p_bounds.add_argument("inputs", nargs="+")
    p_bounds.add_argument("--unlabelled-only", action="store_true",
                          help="skip boundaries already in tests/labels.json")
    p_bounds.add_argument("--gap", type=float, default=0.5,
                          help="seconds either side of the cut (default 0.5)")
    p_bounds.add_argument("--width", type=int, default=560,
                          help="width of each frame (default 560)")
    p_bounds.add_argument("--open", action="store_true",
                          help="open the folder when finished")
    add_render_options(p_bounds)
    p_bounds.set_defaults(func=cmd_boundaries)

    p_set = sub.add_parser("settings", help="show or change saved settings")
    p_set.add_argument("--set", nargs="*", metavar="KEY=VALUE")
    p_set.set_defaults(func=cmd_settings)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except OSError as exc:
        # Most often data/ cannot be created - a protected folder, or a
        # read-only drive. A traceback here would be readable but would still
        # bury the one thing worth saying.
        print(f"\nCannot write to the data folder: {exc}", file=sys.stderr)
        print("Move the app somewhere you can save files, then try again.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
