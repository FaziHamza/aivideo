# Reaction Video Builder

Takes one long video made of separate clips, detects the clip boundaries, drops
exactly one reaction after each clip, and writes a 3-minute 9:16 720x1280 MP4.
Built to run 50-60 videos a day as a queue.

## Run it

```bat
run.bat                              :: desktop app
.venv\Scripts\python.exe main.py     :: same thing
.venv\Scripts\python.exe cli.py --help
```

First time only:

```bat
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

FFmpeg and FFprobe must be on `PATH`. Nothing in `requirements.txt` is
mandatory: without PySide6 the CLI still renders, and without PySceneDetect
boundary detection falls back to FFmpeg's own scene filter.

Check the environment any time:

```bat
.venv\Scripts\python.exe cli.py doctor
```

## How to use it

The window has two lists and they are not interchangeable:

- **STEP 1, left: Reaction library.** The SHORT clips (1-3 seconds) that get
  inserted after every cut. Added once and saved forever, so you never touch
  this panel again unless you want different reactions. Use the `Add files...`
  button at the *bottom left*.
- **STEP 2, right: Input videos.** The LONG videos (3 minutes or more) that get
  cut into clips. One input video produces one output video. Use the
  `Add videos...` button under that list.

So the everyday run is:

1. First time only: `Add files...` on the left, pick your reaction clips.
2. `Add videos...` on the right, pick the long video(s). Each queued video shows
   its length; anything too short to fill the target is flagged in red.
3. Optional: `Preview timeline` to see which clips were found and which
   reactions will land where, without encoding.
4. `Start rendering`. Progress runs per video and per queue; each finished row
   in Results shows the length, size, time taken and a pass/fail verdict.
   Double-click a row to open the file.

Putting reaction clips in the input list is the easy mistake to make - they are
too short to fill a 3-minute video, so the result would be a 2-second file. The
app now spots anything shorter than the target and offers to put it in the
reaction library instead.

If every reaction is unticked, rendering is blocked and a red banner says so;
`Tick all` puts them back.

## Layout

```
core/          the engine - plain Python, no Qt, no web
  ffmpeg.py      probe / run / encoder selection / filter building
  scenes.py      clip-boundary detection (PySceneDetect or FFmpeg)
  planner.py     which clips go in, in what order, to hit 3:00
  renderer.py    encoding, the reaction cache, concat
  library.py     SQLite reaction library + job history
  validate.py    proves the finished file is 9:16 720x1280 and the right length
  pipeline.py    orchestration and batching
  vision.py      Asks the model whether each boundary is a real cut
desktop/       PySide6 window - imports core, owns no logic
cli.py         command line - imports core, owns no logic
data/          settings.json, library.db, output/, cache/, temp/
```

The engine is deliberately UI-independent. The desktop app and the CLI are both
thin shells over `core`, which is what makes a web front end a matter of adding
a third shell rather than rewriting anything.

## How the 3 minutes are hit

For a given number of clip+reaction pairs the reactions are already fixed by the
rotation, so their total is a constant. That leaves "pick N clips whose lengths
land closest to the remaining budget" - an exact-k subset-sum, solved with a
bitset DP. A 300-clip input plans in about 20 ms.

Two details matter for landing on 3:00 rather than near it:

- **Cuts are found by colour content, not frame difference.** See below; this
  is what decides how many reactions the video gets.
- **Everything is planned in whole output frames.** An encoder can only emit
  whole frames, so planning in seconds and rendering in frames drifts. At 30fps
  a 20-segment timeline lost a quarter of a second that way.
- **Reaction lengths come from the video track, not the container.** For a
  1.5-second clip the AAC track is often longer than the video, so the container
  duration overstates how many video frames exist and the encoder silently comes
  up a frame short. Reactions are frame-counted exactly when added.

Result on the sample input: planned 5400 frames, rendered 5400 frames, 180.0000s
of video. The container reports 180.021s because the AAC tail rounds up to its
own frame size, which is inaudible and unavoidable.

Only the final pair is ever shortened - the last reaction first, then the last
clip if the reaction alone cannot absorb the overshoot - and only when "Shave
the last reaction" is on. Nothing in the body of the video is touched. Turn it
off and every clip stays whole, landing within the tolerance instead.

**Clip order** decides which clips go in. `sequential` (default) walks them in
order, one reaction each, until the target is full: the video plays through in
order and every clip used gets exactly one reaction. `fit` instead picks
whichever clips add up closest to 3:00, which lands the length more precisely
but skips around the video. With `sequential`, if the whole video's clips add up
to less than the target the output is simply shorter, and says so.

## Where the model is used, and where it is not

Two jobs need a model. Finding the cuts does not.

| Job | Model? | Why |
|---|---|---|
| Finding candidate cuts | No | A measurement. The histogram detector does it, for free, in about 12 seconds - and when the cut check is live it deliberately over-proposes (recall profile), because the check can delete a wrong boundary but can never invent a missed one. |
| **Which of those are real cuts** | **Yes** | A judgement. With every boundary of the labelled video graded, the pixel measures alone split 7 of 24 continuous shots down the middle - a reaction landing mid-shot. The shipped path (recall detection + the check) keeps 12 of 17 real cuts and splits 1-2 of 24 shots. On by default. |
| **Which clips are worth reacting to** | **Yes** | A judgement about content. No pixel statistic decides whether a clip is funny, so this is what the model is for. |

The middle row was "optional, off by default" until every boundary got
labelled. It was optional on the strength of a measurement that only covered
half the detector's output; the uncovered half is where the mistakes were.

`clip_selection: best` (the default) rates every clip and uses the
highest-rated ones that fit, keeping them in chronological order. On the sample
input it picked 17 clips of 21 and dropped the ones it described as *"static
street, person walking"*, while scoring *"backhoe rolls off trailer onto
truck"* an 8.

Ratings come from XtroEdge AI via `POST /messages`. Each clip becomes a small
filmstrip - three frames side by side - and a whole video is **one request,
about 4,600 tokens, roughly 20 seconds**. The cut check is a second call
pattern: a six-frame strip of the second around each boundary, sixteen strips
to a request, so a 3-minute input is **about three requests and 50 seconds**.

Together that is ~4 requests and ~12,000 tokens a video. At 60 videos a day:
**~240 of the key's 500 daily requests (48%) and ~720k of its 1,000,000 tokens
(72%)**. Both jobs check the remaining budget before spending (the reading is
cached a few minutes so the check itself stays nearly free) and stand down
rather than failing mid-batch - and if the cut check dies mid-video, detection
is redone at the conservative profile instead of shipping boundaries nobody
reviewed.

Two details that matter for reliability:

- **It cannot fail a render.** No key, no request budget left, a network error,
  a reply that does not parse: the planner falls back to taking clips in order
  and the job notes say why. `rank_min_requests` reserves budget so a long
  batch does not stop partway through for quota.
- **The key is never in the code.** It comes from `XTROEDGE_API_KEY` or
  `data/xtroedge_key.txt`, which sits inside the gitignored `data/` directory.
  `Tools > Environment check` shows where it was found and the live quota,
  never the key itself.

One integration quirk worth knowing: the gateway is behind Cloudflare, which
rejects the standard library's default user agent with `403 error code: 1010`
before the request reaches the API. That looks exactly like a bad key and is
not - a `{"detail": ...}` body means the API answered, a bare `error code: NNNN`
means the CDN did.

## Finding the cuts

One reaction goes after each clip, so the number of reactions is decided
entirely by how many clips the detector finds. Getting that wrong is the most
visible way this tool can fail.

FFmpeg's `scene` filter and PySceneDetect's `ContentDetector` both score how
much changes between *neighbouring frames*. That cannot separate a cut from
fast camera motion - a pan across a bright scene moves as many pixels as a cut
does. On the sample footage it called 17 of 27 boundaries a cut when the shot
had not actually changed, so single clips were split into three and each piece
was handed its own reaction. Watching it back, that reads as two or three
reactions inside one clip. Miss cuts instead and you get the opposite
complaint: one 46-second "clip" holding three scenes and a single reaction.

The `histogram` detector compares the third of a second before a candidate
against the third of a second after it - a pan or a shake barely changes that,
a cut changes it a lot - and compares a **4x4 grid of colour histograms**
rather than one histogram for the whole frame. A whole-frame histogram only
knows which colours are present, so an outdoor shot of an orange digger and an
indoor shot against an orange wall look nearly identical to it; that was a real
cut in the sample footage scoring 0.31, below any threshold that did not also
split clips in half. The grid also knows roughly where the colours are.

### The threshold is not a hardcoded number

Every video is different, so a threshold calibrated on one of them is a guess
about the next. `cut_strength` therefore defaults to **auto**: it reads the
split off each video's own score spread using Otsu's method, then sits a little
above it, because Otsu's lower group mixes "nothing happening" with "camera
moving" and camera movement is the one thing that must not read as a cut.

Across six test videos the automatic value came out at 0.45-0.50 - the same
place hand-calibration had landed, but derived per video rather than assumed.
Set a number in place of `auto` to override it.

### How it was checked without trusting my own eyes

Two measurements, because the first one is not reliable on its own.

**Constructed ground truth.** Known segments were cut out of the footage and
glued together, so every join is a real cut at a known timestamp and there are
no others. Four variants - even 6s clips, mixed lengths, five long clips, and
fourteen 2s clips - giving 35 known cuts. The auto threshold finds 32 of 35.

**Labelled boundaries.** 24 boundaries in the real footage, each judged from a
full-size frame 0.5s either side: 12 real cuts scoring 0.517-0.761, 12
same-scene boundaries scoring 0.168-0.428. The auto value lands in that gap and
splits none of the 12 same-scene boundaries.

Worth recording how nearly that second measurement went wrong. Three of those
boundaries were first labelled from 200px thumbnails 0.25s apart, which was not
enough to see what was happening - they looked like the same scene and were in
fact cuts. Counted that way the measure appeared to overlap, and the conclusion
drawn from it was that you had to choose between missing real cuts and
splitting shots in half. Re-checking at full size showed the measure separates
cleanly and no such choice existed. When a measurement says two things cannot
be had at once, check the labels before believing it.

### Which numbers come from where

Nothing in the detector is tuned to a particular video:

| Value | Where it comes from |
|---|---|
| `cut_strength` | auto, per video, from its own score spread |
| 0.3s window, 0.1s gap | how long a cut takes, not what is in the frame |
| 64px, 4x4 grid, 8 bins | resolution of the measure; content-independent |
| auto margin 1.15, clamp 0.35-0.65 | measured across six videos; guards |
| `min_clip_duration` 1.5s | how short a clip is worth showing - your call |
| `ffmpeg_scene_threshold` 0.20 | calibrated on the sample footage, and only used by the non-default `ffmpeg` detector |

Two more details that mattered:

- **Position comes from the jump, the verdict from the contrast.** The two
  signals peak on different frames - the strongest before/after contrast is
  often a few frames early, where nothing is moving yet. Reading the position
  off the contrast peak put cuts up to 0.9s late, bleeding the end of one scene
  into the start of the next. Taking the position from the frame the picture
  jumps on holds it inside 0.24s.
- **No arbitrary splitting.** `max_clip_duration` can split an over-long clip
  at its most cut-like inner point, but it is off by default: splitting a
  genuinely continuous shot puts a reaction in the middle of it, which is the
  same complaint as a missed cut wearing a different hat.

It all works from one decode pass at the output frame rate, so it costs about
what the old detector cost and every cut already lands on an output frame.

`cut_strength` is the knob if your footage differs: raise it for fewer, longer
clips, lower it for more. **Preview timeline** shows the result without
encoding.

## Speed

Measured on this machine (AMD GPU, `h264_amf`), 256s 1080p input, 3-minute
output:

| Detector | Detection | Clips found |
|---|---|---|
| `histogram` (default) | 10.5s | 27 |
| `ffmpeg` | 13.6s | 30 |
| `content` | 59.9s | 37 |
| `adaptive` | 57.4s | 22 |

Whole video end to end, everything on, at 1080x1920: **about 2.5 minutes**,
of which roughly 50s is the cut check and 20s the clip rating - network time,
not CPU. At 60 videos that is about **2.5 hours** a day. Dropping the frame to
720x1280 or turning the model jobs off both roughly halve it, at the cost of
either the resolution or the accuracy in the table further up.

Output is **1080x1920** (the frame Shorts/Reels/TikTok serve) at a bitrate
scaled to the frame area - `balanced` is ~5.6 Mbps, about 125 MB per 3-minute
video. The presets are calibrated in bits per pixel, so changing `width` and
`height` in settings.json re-derives sane rates instead of stretching a
720p bitrate over a 1080p frame. The crop path upscales with lanczos, because
cropping 16:9 to 9:16 keeps a 607px column of a 1080p source and the default
bilinear goes visibly soft blowing that back up.

Two things do the heavy lifting:

- **Hardware encoding.** NVENC, QSV and AMF are each smoke-tested with a
  6-frame encode at startup, because being listed by FFmpeg does not mean the
  driver will accept it. First one that works wins; `libx264` is the fallback,
  and a segment that a hardware encoder refuses is retried on `libx264`
  automatically.
- **The reaction cache.** Each reaction is normalised to 720x1280 once and
  reused across every video, so a day of rendering encodes them once rather
  than 55 times. Change the resolution, fit mode or quality and the cache
  rebuilds itself; `Tools > Rebuild reaction cache` forces it.

Hardware encoders are driven by explicit bitrate rather than a quality target,
because their quality modes ignore `-maxrate` on some drivers - that produced a
340 MB file for 3 minutes. Bitrate mode keeps it at about 57 MB on `balanced`.

## Settings that actually change the output

| Setting | Default | Why you would change it |
|---|---|---|
| Target length | 180s | Any length; the planner adapts |
| Shave last reaction | on | Off keeps every clip whole, accepts small drift |
| Fit non-9:16 | `crop` | `blur` keeps the whole frame but adds bands; `pad` is fastest |
| Quality | `balanced` | ~57 MB; `high` ~102 MB, `small` ~33 MB |
| Detector | `histogram` | Only one that ignores camera motion; see above |
| Cut strength | `auto` | Derived per video; set a number to override |
| Frame | 9:16, 1080p | Two pickers: ratio (9:16 / 16:9 / 1:1) and resolution (1080p / 720p). The resolution is the short side, so 1080p means 1080x1920 portrait and 1920x1080 landscape. A hand-edited size in settings.json shows as `custom` and is left alone |
| Clip order | `best` | `sequential` = in order, no model; `fit` = closest length |
| Clip rating | on | Needs an XtroEdge key; falls back to clip order without one |
| Cut check | on | Verifies every boundary; needs the same key. Off means reactions can land mid-shot |
| Min clip length | 1.5s | Drops junk fragments |

`crop` is the default because it is the only mode that treats every source the
same. This input is landscape 1920x1080 and one of the eleven reactions is too,
while the other ten are portrait 1080x1920 - under `blur` the portrait reactions
fill the frame and everything else gets bands above and below, so the output
visibly changes shape as it plays. `crop` fills the frame for all of them. The
cost is the sides: cropping 16:9 to 9:16 keeps the middle third of the width.

## Validation is against the target, not just the plan

A plan that ran out of material is internally consistent - a 3-second file for a
3-second input is exactly what was planned - so checking the render only against
its own plan will pass a 3-second video as fine. Validation therefore checks
both: the file matches the plan *and* the file is the length that was asked for.

That matters in practice because queueing a reaction clip as an input is an easy
mistake. The queue now shows every input's length and flags in red anything too
short to fill a video, the pipeline says so before it starts rendering, and the
result is reported as a failure with the reason rather than a green OK.

## Reaction library

Stored in `data/library.db` and kept across restarts. Add files or a whole
folder, replace the lot in one step, untick one to hold it out of rotation, or
reorder to change the rotation order. Reactions repeat in rotation when there
are fewer of them than there are clips, and the rotation carries over between
videos in a queue so consecutive outputs do not all open with the same reaction.

## The window

`desktop/theme.py` holds the whole look as one Qt stylesheet, kept out of the
app code so a design change cannot break a signal connection. It carries the
XtroEdge palette as given: primary green `#4ADE80 -> #16A34A`, charcoal text
`#374151`, white `#FFFFFF`, tagline gray `#6B7280`. Light theme.

Two things about the green are deliberate and easy to undo by accident:

- **The gradient appears in two places only** - the header bar and the
  progress chunk - and everywhere white text sits on green it is flat
  `#16A34A`. `#4ADE80` is too light to carry white type; using it for buttons
  would fail contrast. The light end is for the sweep and for hover tints.
- **The greys are the brand greys, extended rather than replaced.** `#374151`
  and `#6B7280` are Tailwind's gray-700 and gray-500, so borders and
  backgrounds use the rest of that ramp. Any other grey reads as a second,
  slightly-off palette.

The header is white and carries the official logo - the one with the charcoal
wordmark, `xtroedge-logo-colour.png`, taken from the same server as the rest.
A green header was built first, using the white-wordmark variant, and it
worked; it was replaced because it meant showing a different logo from the one
on the website. The green moved to a 3px rule under the bar instead, which is
also more honest to "light theme".

The icon is the X mark on its own (`xtroedge-symbol.png`) on a white rounded
square with a faint edge: the wordmark is unreadable at 16px, and the faint
edge is what keeps the square a shape on a light taskbar while the green mark
carries it on a dark one. Rendered to `assets/app-icon.png` for the window and
taskbar and `app-icon.ico` for the .exe. All four images are bundled by
`app.spec`, and `theme._asset` checks beside the .exe first, so the branding
can be swapped without a rebuild.

Semantic colour is reserved for outcomes - brand green for a pass, red for a
failure, amber for "this will not do what you think" - because those are the
only three things worth interrupting someone for.

The key goes in the window, not in a text file by hand. Tick either AI check
and an `XtroEdge key` row appears with Save and Test; untick both and it goes
away, since that is the only time it does anything. What it shows is never the
key: at most the last four characters, enough to tell two keys apart, because
this window gets screenshotted and screen-shared. `Test` asks the API what
quota is left and turns it into "roughly N more videos".

Four things in here are guards rather than features, each one a bug that had
already happened:

- **Nothing is saved while the window is being built.** A combo box emits a
  change as soon as its items are added, and the save handler then read
  defaults off widgets that did not exist yet and wrote those over the user's
  file. `_ui_ready` is only set once construction and loading are both done.
- **The mouse wheel does not edit a control it is only passing over.** Qt's
  default let a scroll change whatever was under the pointer: `min clip
  length` went from auto to 4.5s with nobody touching it, saved, and applied
  to the next render. Values now change only when a control has focus.
- **One window at a time.** Two share one `settings.json`, one `library.db`
  and one output folder, and the second to close overwrites the first. That
  is what "the app forgot my setting" turned out to be. A `QLockFile` holds
  it; the second copy says so and exits.
- **The settings column scrolls.** It is taller than a small screen once the
  key row shows, and without a scroll area Qt squeezes the grid past its
  minimum until the rows draw on top of each other.

## Batching

Videos are processed strictly one at a time. A failure on one is recorded and
the queue carries on, so an overnight run of 60 does not die on video 12.
`Tools > Show render history` lists what was produced, and the status bar tracks
the day's count against the 50-60 target.

## The cut check

When the check is live, detection deliberately over-proposes: on the labelled
video the recall profile puts up ~40 candidate boundaries and catches 16 of
the 17 real cuts, where the conservative profile proposes 20 and catches 13.
Every candidate then goes to the vision model as a six-frame filmstrip of the
second around it, and the question is whether the picture switches shots
somewhere in that strip or is one continuous piece of motion. Boundaries
judged continuous are merged, so the reaction moves to the end of the real
shot instead of landing inside it.

Measured on `tests/labels.json`, all 24 same-scene and 17 cut boundaries:

| | false splits | real cuts kept |
|---|---|---|
| Pixel measures alone (no key) | 7 of 24 | 13 of 17 |
| Shipped: recall profile + check | **1-2 of 24** | 12 of 17 |

The range is honest: the gateway rejects `temperature`, so verdicts move by
about one boundary between identical runs, and the test thresholds allow for
that. The trade is the one this project asked for: a missed cut leaves two
scenes in one clip and reads as ordinary, a false split drops a reaction
mid-shot and reads as broken.

Things learned building it, each paid for with a wrong video:

**Every boundary, not the doubtful ones.** Sending only low-confidence
boundaries was the first design. It does not work: the confidence score put 2
of 21 boundaries in the doubtful band while 7 were wrong, because real cuts and
fast camera moves score in the same range. The score does not know which ones
it got wrong, so there is nothing to select on.

**Two frames cannot show that a change was instant, and instant is what "cut"
means.** The first design sent one frame from either side, 0.5s apart. A real
cut to a wider view of the same subject then looks exactly like a fast pan,
and whichever way the prompt leaned, one of the two got misjudged - "camera
setup changed = cut" left 3 pans split, "same subject = same" merged 4 real
cuts. Six frames 0.2s apart carry the answer in the steps: a pan changes
gradually across all of them, a cut switches between two adjacent ones. Four
frames clustered at the boundary and eight small ones across 1.4s were both
tried and both worse - the strip has to cover the detector's position error
(up to ~0.4s) while keeping frames big enough to read.

**Over-proposing is only safe while the verifier is actually there.** The
recall profile exists on the promise that its extra boundaries get reviewed.
So it is only used when the key is present and budget remains, and if the
check dies mid-video (network, quota), the pipeline re-detects at the
conservative profile rather than shipping boundaries nobody reviewed.

**It cannot fail a render.** No key, an HTTP error, a reply that does not
parse: the render still completes, on conservative boundaries, and the job
notes say so. Switch it off with `vision_enabled` and the app says
`cut check OFF` in the status bar, because that is a decision worth seeing.

## Web front end

Not built yet. The engine is already UI-independent, so it means adding a
`web/` shell next to `desktop/` - a job queue, progress over SSE, and the same
`Pipeline` calls. Worth knowing before starting: browser upload of a 372 MB
input is the slow part, and 50-60 renders a day is real CPU that costs money on
a hosted box but nothing locally.

## Packaging

```bat
build.bat            :: dist\ReactionVideoBuilder\ReactionVideoBuilder.exe
build.bat --zip      :: same, plus a .zip to hand over
build.bat --slim     :: without FFmpeg (~420 MB smaller, user installs it)
```

PyInstaller, one folder rather than one file: a single-file build of Qt plus
OpenCV unpacks ~600 MB to a temp directory on every launch, which is ten to
twenty seconds before the window appears. The folder starts instantly.

Two things the build has to get right, both of which are silent if wrong:

- **`data/` lives next to the .exe, not inside the bundle.** Frozen, `__file__`
  points into PyInstaller's unpacked payload, which is temporary. Left alone,
  the reaction library and settings would be rebuilt from nothing on every
  launch. `config.PROJECT_ROOT` therefore reads `sys.executable` when frozen.
- **FFmpeg and FFprobe are copied into the bundle.** `core/ffmpeg.py` looks in
  the bundle and beside the .exe before it looks at `PATH`, so the machine it
  is handed to needs nothing installed. This is most of the size: the two
  static binaries are 420 MB of the 643 MB.

Not code-signed, so Windows SmartScreen shows "Windows protected your PC" on
first run - More info, then Run anyway. `READ ME FIRST.txt` in the folder says
so.

Built and smoke-tested: 643 MB as a folder, 246 MB zipped. The .exe starts,
creates its `data/` beside itself, and the bundled FFmpeg encodes with
`h264_amf`. The build carries no key, no reaction library and no settings -
verified, because all three live in `data/` and a shared build is the wrong
place for a per-customer key. `READ ME FIRST.txt` explains where to put it and
what is lost without it: not just clip order, but the cut check, and without
that a reaction can land mid-shot.

## Tests

```bat
run_tests.bat                                    :: everything
.venv\Scripts\python.exe -m tests.run_all planner   :: one suite
.venv\Scripts\python.exe -m tests.run_all --rebuild :: rebuild fixtures
```

Three suites, and the reason each exists:

- **planner** - every timeline rule as arithmetic, no footage needed. One
  reaction per clip, chronological order, no clip reused, rotation intact, only
  the final pair ever trimmed, exact 3:00.
- **detection** - scored three ways, because none is enough alone. *Recall*
  against constructed video (known segments glued together, so every join is a
  known cut and there are no others) catches a detector that stops finding
  cuts. *Precision* against `tests/labels.json` (boundaries judged by eye from
  full-size frames) catches a detector that finds cuts that are not there,
  which constructed truth cannot measure. Precision is reported twice, once
  for the pixel measures alone and once for the shipped path with the cut
  check, so it stays visible how much of the accuracy is bought with API calls
  rather than computed. Pass `--offline` to skip the half that spends quota.
- **render** - renders a video and asserts *planned frames == frames written*.
  That is the only form of "the output is 3 minutes" that cannot quietly be
  false.

The false-split ceiling is deliberately the strict number and the missed-cut
floor is not: a missed cut leaves one clip holding two scenes and one reaction,
which reads as ordinary, while a false split drops a reaction into the middle of
a shot, which reads as broken.

**The detection suite checks every boundary the detector produces, not a
sample, and prints the ones that are not labelled yet.** That is the part worth
keeping. An earlier round of this work verified only the boundaries that
happened to be labelled while the detector produced about twice as many - so
half of what it decided went unexamined, and the unexamined half was where the
bug was. Labelling the boundaries it lists makes the test stricter; ignoring
them leaves a known blind spot visible instead of hidden.

## Long inputs

A stretch of footage longer than the whole target output is unusable as one
clip, and it used to fail loudly on someone else's machine and quietly here: a
7-minute input with few detectable cuts shipped with one reaction near the
start and nothing after it. The planner had skipped or swallowed the giant
clip. Two layers now make that impossible:

- The detector chops any stretch longer than the target into ~45s pieces at
  its most cut-like inner moments (candidates below the cut threshold count).
  Only stretches that exceed the target are touched - a legitimate 90s clip
  in normal footage is left alone.
- After the vision check (which can merge boundaries back together), the
  pipeline re-checks and evenly splits anything still longer than the target.
  These splits are marked `forced` and the vision check skips them, because
  it would correctly call them mid-shot and undo the only structure keeping
  the video usable.

`tests/test_long_input.py` holds the bar with a 7-minute zero-cut synthetic:
every clip inside the target, plan on target, at least 4 reactions. It runs
offline - this regime must work on a machine with no API key, because that is
where it failed.

Every job also writes its decisions to `data/logs/app.log` (rotated at 2 MB,
never contains the key) and stores its notes in the jobs table - because this
bug was reported from a machine we could not see, and the only fix for that
class of problem is a file the client can send.

## Known limits

- A clip longer than the target cannot be used whole and is skipped, with a note.
- If the input is one continuous shot, it is treated as a single clip rather
  than inventing cuts.
- The tail reaction is never shaved below 1 second, so a very short one may
  leave the output a little over target. The planner prefers a pair count whose
  tail has room.
- One labelled boundary (168.57s, a camera swinging from a trailer to a van in
  half a second) is still split. It is also the least certain label in the set;
  the tests list it rather than quietly excluding it.
- Four of the 17 labelled cuts are missed before the cut check runs and five
  after. The clip they leave holds two scenes and gets one reaction, which is
  the failure this project chose to accept.
- The in-order pick cannot always reach exactly 3:00. When it lands short the
  planner switches to choosing clips that fit, still chronological and still
  one reaction each - the note says when that happened. Without that fallback
  one reaction rotation in eleven produced a 177.5s video.
