"""PySide6 desktop app: the MVP front end for the reaction-video engine.

Screen layout mirrors the workflow in the requirements: pick one long input (or
a queue of them), keep a persistent reaction library on the left, set the output
format, then watch a progress bar per video and a validated result per row.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QLockFile, QObject, Qt, QUrl
from PySide6.QtGui import (QAction, QDesktopServices, QFont, QIcon,
                           QGuiApplication, QKeySequence, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QSplitter,
                               QTableWidget,
                               QTableWidgetItem,
                               QTextEdit, QVBoxLayout, QWidget)

from core import ffmpeg, planner
from core.config import DATA_DIR, TEMP_DIR, Settings, ensure_dirs
from core.library import VIDEO_SUFFIXES, ReactionLibrary
from core.models import JobResult, RenderPlan
from core.pipeline import Pipeline
from desktop import theme
from desktop.worker import AddReactionsWorker, BatchWorker, PlanWorker

_MEDIA_FILTER = ("Video files (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts "
                 "*.mpg *.mpeg *.wmv *.flv);;All files (*)")

# Frame presets for the two pickers. The resolution number is the SHORT side
# (that is what 720p/1080p means regardless of orientation), so 1080p is
# 1080x1920 portrait but 1920x1080 landscape. Values, not formulas, so every
# frame the app can produce is spelled out and even-sized for the encoder.
_FRAMES: dict[tuple[str, str], tuple[int, int]] = {
    ("9:16", "720p"):  (720, 1280),
    ("9:16", "1080p"): (1080, 1920),
    ("16:9", "720p"):  (1280, 720),
    ("16:9", "1080p"): (1920, 1080),
    ("1:1", "720p"):   (720, 720),
    ("1:1", "1080p"):  (1080, 1080),
}
_CUSTOM = "custom"


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds) // 60}:{seconds % 60:04.1f}"


class WheelGuard(QObject):
    """Stops the mouse wheel changing a value it is only passing over.

    Qt's default is that a spin box or combo under the pointer eats the wheel
    and steps its value, so scrolling the settings panel silently edits
    whatever the cursor happened to be over. That is how `min clip length`
    went from auto to 4.5s here with nobody touching it - a real setting
    changed by a scroll, saved, and applied to the next render. Values now
    change only when the control has focus, i.e. when it is being edited.
    """

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and not watched.hasFocus():
            event.ignore()
            return True
        return super().eventFilter(watched, event)


class PlanDialog(QDialog):
    """Shows the timeline the planner chose, before committing to a render."""

    def __init__(self, plan: RenderPlan, source: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Planned timeline - {source.name}")
        self.resize(680, 560)
        layout = QVBoxLayout(self)

        summary = QLabel(planner.describe_plan(plan))
        summary.setWordWrap(True)
        layout.addWidget(summary)

        table = QTableWidget(len(plan.segments), 5, self)
        table.setHorizontalHeaderLabels(["#", "Kind", "Label", "Source start",
                                         "Length"])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        for row, seg in enumerate(plan.segments):
            length = f"{seg.out_duration:.2f}s"
            if seg.is_trimmed:
                length += "  (shaved)"
            for col, text in enumerate([
                str(row + 1), seg.kind, seg.label,
                f"{seg.clip.start:.2f}s", length,
            ]):
                table.setItem(row, col, QTableWidgetItem(text))
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.resizeColumnsToContents()
        layout.addWidget(table)

        close = QPushButton("Close", self)
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_dirs()
        self.settings = Settings.load()
        self.library = ReactionLibrary()
        self.batch: BatchWorker | None = None
        self.planner_thread: PlanWorker | None = None
        self.reaction_thread: AddReactionsWorker | None = None
        self._loading_reactions = False
        # Loading saved values into the widgets fires their change signals; the
        # handler must not write half-loaded state back over settings.json.
        self._loading_settings = False
        # Nor may it write while the window is still being built. A combo box
        # emits a change as soon as its items are added, which reaches the
        # save handler before the later widgets exist - so the handler would
        # read the defaults off half-built widgets and save those over the
        # user's file. This has now bitten twice, hence a flag that is only
        # lifted once construction and loading are both finished.
        self._ui_ready = False
        # Kept on the window so the filter outlives the widgets it watches.
        self._wheel_guard = WheelGuard(self)
        self._results: list[JobResult] = []

        self.setWindowTitle("Reaction Video Builder")
        icon = theme.icon_path()
        if icon is not None:
            self.setWindowIcon(QIcon(str(icon)))
        # Sized for the settings grid at its tallest, which is with the key
        # row showing. A minimum as well as a default: without one the grid
        # rows collapse on top of each other rather than the window refusing
        # to shrink, which reads as a broken window instead of a small one.
        # Small enough that no screen can force the layout past its minimum,
        # which is what made the settings rows draw on top of each other.
        # Everything above this size is handled by the scroll area.
        self.setMinimumSize(820, 480)
        self._size_to_screen(1280, 900)
        self.setAcceptDrops(True)

        self._build_ui()
        self._load_settings_into_ui()
        self._ui_ready = True      # from here on, edits are the user's
        self.refresh_reactions()
        self._refresh_status()

    def _size_to_screen(self, want_w: int, want_h: int) -> None:
        """Open at the wanted size, or the screen's, whichever is smaller.

        Asking for a 900px-tall window on a screen with 680px of usable
        height puts the status bar and the results table below the bottom
        edge, where nobody finds them. The panel scrolls, so a short window
        costs nothing.
        """
        # The roomiest screen, not the primary one. This machine's primary is
        # 1280x680 of usable space at 150% Windows scaling, which leaves the
        # app about 850x450 to draw in - less than the settings grid needs,
        # so it opened clipped while a 1920x1040 monitor sat empty beside it.
        screens = QGuiApplication.screens()
        if not screens:
            self.resize(want_w, want_h)
            return
        screen = max(screens, key=lambda sc: (sc.availableGeometry().width()
                                              * sc.availableGeometry().height()))
        area = screen.availableGeometry()
        width = min(want_w, area.width() - 20)
        height = min(want_h, area.height() - 20)
        self.resize(max(width, 820), max(height, 480))
        self.move(area.x() + max(0, (area.width() - self.width()) // 2),
                  area.y() + max(0, (area.height() - self.height()) // 2))
        # Deliberately not showMaximized() here: maximising during
        # construction pins the window to the primary screen before the move
        # above has taken effect, which put it back on the small one. The
        # scroll area means a window smaller than the layout scrolls rather
        # than clipping, so the size chosen above is safe on any screen.

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_header(self) -> QWidget:
        """The brand bar: white logo on the green gradient.

        The supplied logo is white on transparent, so it is only legible with
        the brand green behind it - which is why this is a filled bar rather
        than a logo dropped onto the window background.
        """
        wrap = QWidget(self)
        stack = QVBoxLayout(wrap)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)

        bar = QWidget(wrap)
        bar.setObjectName("brandHeader")
        bar.setFixedHeight(58)
        row = QHBoxLayout(bar)
        row.setContentsMargins(18, 0, 18, 0)
        row.setSpacing(14)

        path = theme.logo_path()
        if path is not None:
            mark = QLabel(bar)
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                # Scaled by height and smoothly: the source is 1000x280, and
                # a nearest-neighbour downscale of white type looks broken.
                # 36px, not less: the logo carries its own small tagline
                # under the wordmark, and below about 34 that line turns to
                # mush. The bar is 58px, so this still clears the edges.
                mark.setPixmap(pixmap.scaledToHeight(
                    36, Qt.TransformationMode.SmoothTransformation))
                row.addWidget(mark)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        title = QLabel("Reaction Video Builder", bar)
        title.setObjectName("brandTitle")
        tagline = QLabel("One long video in, a 3-minute reaction cut out",
                         bar)
        tagline.setObjectName("brandTagline")
        text.addWidget(title)
        text.addWidget(tagline)
        row.addLayout(text)
        row.addStretch(1)

        # A 3px brand-green rule under the white bar: the palette stays
        # present without putting charcoal type on green.
        rule = QWidget(wrap)
        rule.setObjectName("brandRule")
        rule.setFixedHeight(3)

        stack.addWidget(bar)
        stack.addWidget(rule)
        return wrap

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._build_reactions_panel())
        # The right-hand column is taller than a 1366x768 laptop screen once
        # the key row shows. Without a scroll area Qt squeezes the grid past
        # its minimum and the rows draw on top of each other, which looks
        # like a broken window rather than a small one.
        scroller = QScrollArea(self)
        scroller.setWidget(self._build_main_panel())
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QScrollArea.Shape.NoFrame)
        scroller.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        splitter.addWidget(scroller)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 860])
        # Let someone on a narrow screen shrink the library panel right down
        # rather than losing the settings column off the right edge.
        splitter.setChildrenCollapsible(True)

        shell = QWidget(self)
        stack = QVBoxLayout(shell)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)
        stack.addWidget(self._build_header())
        body = QWidget(shell)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 8, 10, 8)
        body_layout.addWidget(splitter)
        stack.addWidget(body, 1)
        self.setCentralWidget(shell)

        tools = self.menuBar().addMenu("&Tools")
        act_purge = QAction("Rebuild reaction cache", self)
        act_purge.triggered.connect(self.purge_cache)
        tools.addAction(act_purge)
        act_history = QAction("Show render history", self)
        act_history.triggered.connect(self.show_history)
        tools.addAction(act_history)
        act_doctor = QAction("Environment check", self)
        act_doctor.triggered.connect(self.show_doctor)
        tools.addAction(act_doctor)

    def _build_reactions_panel(self) -> QWidget:
        box = QGroupBox("STEP 1 - Reaction library  (the SHORT clips)")
        outer = QVBoxLayout(box)

        hint = QLabel("Short clips inserted after every cut. Saved permanently, "
                      "so you only add them once. Untick to skip one; the order "
                      "here is the rotation order.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.reaction_list = QListWidget(box)
        self.reaction_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.reaction_list.itemChanged.connect(self._reaction_toggled)
        delete_key = QShortcut(QKeySequence.StandardKey.Delete,
                               self.reaction_list)
        delete_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_key.activated.connect(self.remove_reactions)
        self.reaction_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.reaction_list.customContextMenuRequested.connect(
            self._reaction_menu)
        outer.addWidget(self.reaction_list, 1)

        self.reaction_summary = QLabel("")
        self.reaction_summary.setObjectName("hint")
        self.reaction_summary.setWordWrap(True)
        outer.addWidget(self.reaction_summary)

        self.reaction_alarm = QLabel("")
        self.reaction_alarm.setWordWrap(True)
        self.reaction_alarm.setStyleSheet(
            f"color: #ffffff; background: {theme.BAD}; padding: 9px 11px; "
            f"border-radius: 7px; font-weight: 600;")
        self.reaction_alarm.setVisible(False)
        outer.addWidget(self.reaction_alarm)

        grid = QGridLayout()
        buttons = [
            ("Add files...", self.add_reaction_files, 0, 0),
            ("Add folder...", self.add_reaction_folder, 0, 1),
            ("Replace all...", self.replace_reactions, 1, 0),
            ("Remove selected", self.remove_reactions, 1, 1),
            ("Move up", lambda: self.move_reaction(-1), 2, 0),
            ("Move down", lambda: self.move_reaction(1), 2, 1),
            # Unticking everything blocks every render, so make getting back
            # from that one click rather than one click per reaction.
            ("Tick all", lambda: self.set_all_active(True), 3, 0),
            ("Untick all", lambda: self.set_all_active(False), 3, 1),
            # Its own button because "select everything, then Remove" cannot
            # be done with ticks: a plain click on any row clears the previous
            # highlight (standard list behaviour), so ticking every box still
            # leaves only the last row selected. People tried exactly that
            # and got one deletion out of eight.
            ("Remove ALL...", self.remove_all_reactions, 4, 0),
        ]
        for text, slot, row, col in buttons:
            btn = QPushButton(text, box)
            btn.clicked.connect(slot)
            if text == "Remove selected":
                btn.setToolTip(
                    "Removes the highlighted clips (the box tick only "
                    "switches a clip in or out of the rotation).\n"
                    "Ctrl+click or Shift+click to highlight several - a "
                    "plain click always highlights just one.\n"
                    "To empty the whole library, use Remove ALL.")
            grid.addWidget(btn, row, col)
        outer.addLayout(grid)
        return box

    def _reaction_menu(self, pos) -> None:
        """Right-click on a clip: the unmissable way to remove one."""
        item = self.reaction_list.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.reaction_list.clearSelection()
            item.setSelected(True)
            self.reaction_list.setCurrentItem(item)
        count = len(self.reaction_list.selectedItems())
        menu = QMenu(self.reaction_list)
        label = ("Remove this clip from the library" if count <= 1
                 else f"Remove these {count} clips from the library")
        act = menu.addAction(label)
        act.triggered.connect(self.remove_reactions)
        menu.exec(self.reaction_list.mapToGlobal(pos))

    def _build_main_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)

        # --- input queue ---
        queue_box = QGroupBox("STEP 2 - Input videos  (the LONG videos to cut up)")
        queue_layout = QVBoxLayout(queue_box)
        queue_hint = QLabel("One long video per output. Each gets cut into clips "
                            "and a reaction dropped after each one. Rendered one "
                            "at a time.")
        queue_hint.setObjectName("hint")
        queue_hint.setWordWrap(True)
        queue_layout.addWidget(queue_hint)
        self.queue_list = QListWidget(queue_box)
        self.queue_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        queue_layout.addWidget(self.queue_list, 1)

        row = QHBoxLayout()
        for text, slot in (("Add videos...", self.add_inputs),
                           ("Add folder...", self.add_input_folder),
                           ("Remove selected", self.remove_inputs),
                           ("Clear", self.clear_inputs)):
            btn = QPushButton(text, queue_box)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        queue_layout.addLayout(row)
        layout.addWidget(queue_box, 1)

        # --- settings ---
        layout.addWidget(self._build_settings_box())

        # --- actions ---
        actions = QHBoxLayout()
        self.btn_preview = QPushButton("Preview timeline", panel)
        self.btn_preview.clicked.connect(self.preview_plan)
        self.btn_start = QPushButton("Start rendering", panel)
        self.btn_start.setObjectName("primary")   # the one accented button
        self.btn_start.setDefault(True)
        font = QFont(self.btn_start.font())
        font.setBold(True)
        self.btn_start.setFont(font)
        self.btn_start.clicked.connect(self.start_batch)
        self.btn_stop = QPushButton("Stop", panel)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_batch)
        self.btn_open = QPushButton("Open output folder", panel)
        self.btn_open.clicked.connect(self.open_output_folder)
        for btn in (self.btn_preview, self.btn_start, self.btn_stop,
                    self.btn_open):
            actions.addWidget(btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        # --- progress ---
        prog_box = QGroupBox("Progress")
        prog_layout = QFormLayout(prog_box)
        self.bar_current = QProgressBar(prog_box)
        self.bar_current.setRange(0, 1000)
        self.bar_overall = QProgressBar(prog_box)
        self.bar_overall.setRange(0, 1000)
        prog_layout.addRow("Current video", self.bar_current)
        prog_layout.addRow("Whole queue", self.bar_overall)
        self.label_status = QLabel("Ready.", prog_box)
        self.label_status.setWordWrap(True)
        prog_layout.addRow("Status", self.label_status)
        layout.addWidget(prog_box)

        # --- results ---
        res_box = QGroupBox("Results")
        res_layout = QVBoxLayout(res_box)
        self.results = QTableWidget(0, 6, res_box)
        self.results.setHorizontalHeaderLabels(
            ["Input", "Output", "Length", "Size", "Time", "Result"])
        self.results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results.verticalHeader().setVisible(False)
        self.results.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.results.doubleClicked.connect(self._open_result_row)
        res_layout.addWidget(self.results)
        layout.addWidget(res_box, 1)

        return panel

    def _build_settings_box(self) -> QWidget:
        box = QGroupBox("Output settings")
        grid = QGridLayout(box)

        self.spin_target = QDoubleSpinBox(box)
        self.spin_target.setRange(10.0, 3600.0)
        self.spin_target.setSuffix(" s")
        self.spin_target.setSingleStep(5.0)
        self.spin_target.valueChanged.connect(self._settings_changed)

        self.chk_exact = QCheckBox("Shave the last reaction to hit it exactly",
                                   box)
        self.chk_exact.stateChanged.connect(self._settings_changed)

        self.combo_ratio = QComboBox(box)
        self.combo_ratio.addItems(["9:16", "16:9", "1:1"])
        self.combo_ratio.setToolTip(
            "The output shape. 9:16 is Shorts/Reels/TikTok; 16:9 is normal "
            "YouTube; 1:1 is a square feed post.")
        self.combo_ratio.currentTextChanged.connect(self._settings_changed)

        self.combo_res = QComboBox(box)
        self.combo_res.addItems(["1080p", "720p"])
        self.combo_res.setToolTip(
            "Output resolution (the short side of the frame). 1080p is what "
            "the platforms serve; 720p renders about twice as fast and the "
            "files are less than half the size.")
        self.combo_res.currentTextChanged.connect(self._settings_changed)

        self.combo_fit = QComboBox(box)
        self.combo_fit.addItems(["blur", "pad", "crop"])
        self.combo_fit.currentTextChanged.connect(self._settings_changed)

        self.combo_quality = QComboBox(box)
        self.combo_quality.addItems(["high", "balanced", "small"])
        self.combo_quality.currentTextChanged.connect(self._settings_changed)

        self.combo_detector = QComboBox(box)
        self.combo_detector.addItems(["histogram", "ffmpeg", "content",
                                      "adaptive"])
        self.combo_detector.setToolTip(
            "histogram compares colour content either side of a cut, so "
            "camera motion is not mistaken for a cut. The others compare "
            "neighbouring frames and split one clip into several on fast "
            "motion.")
        self.combo_detector.currentTextChanged.connect(self._detector_changed)

        self.spin_sensitivity = QDoubleSpinBox(box)
        self.spin_sensitivity.setRange(0.00, 0.90)
        self.spin_sensitivity.setSingleStep(0.05)
        self.spin_sensitivity.setDecimals(2)
        # 0 is not a threshold, it means work one out from the video itself.
        self.spin_sensitivity.setSpecialValueText("auto")
        self.spin_sensitivity.setToolTip(
            "How different the two sides of a cut must look.\n"
            "auto (0) reads it off each video's own score spread, which is "
            "what you want when every video is different.\n"
            "Set a number to override: higher splits into fewer, longer "
            "clips; lower splits into more. Preview the timeline after "
            "changing it.")
        self.spin_sensitivity.valueChanged.connect(self._settings_changed)

        self.combo_selection = QComboBox(box)
        self.combo_selection.addItems(["best", "sequential", "fit"])
        self.combo_selection.setToolTip(
            "best: rate every clip and use the highest-rated ones, so the "
            "reaction lands on the clips worth reacting to. Needs an XtroEdge "
            "key; falls back to sequential without one.\n"
            "sequential: walk the clips in order, one reaction each, until the "
            "target is full. No model needed.\n"
            "fit: pick whichever clips add up closest to the target. Hits the "
            "length most precisely but skips around the video.")
        self.combo_selection.currentTextChanged.connect(self._settings_changed)

        self.spin_min_clip = QDoubleSpinBox(box)
        self.spin_min_clip.setRange(0.0, 30.0)
        self.spin_min_clip.setSuffix(" s")
        self.spin_min_clip.setSingleStep(0.5)
        # 0 is not "no minimum", it means work one out from this video.
        self.spin_min_clip.setSpecialValueText("auto")
        self.spin_min_clip.setToolTip(
            "Clips shorter than this are merged into a neighbour rather than "
            "shown, which is what removes most boundaries that turn out to be "
            "mid-shot.\nauto (0) uses a fraction of this video's median clip "
            "length, so footage cut every two seconds and footage that runs "
            "ten seconds a shot both work.")
        self.spin_min_clip.valueChanged.connect(self._settings_changed)

        # The two AI jobs get their own switches rather than being implied by
        # other settings. They cost requests and they change the output, so
        # whoever is running a batch should be able to see and set them without
        # editing settings.json.
        self.chk_cut_check = QCheckBox("Verify every cut", box)
        self.chk_cut_check.setToolTip(
            "Asks the model whether each detected cut is real, using the frame "
            "half a second either side.\nOn our test footage the picture "
            "measurements alone split 7 of 24 continuous shots down the "
            "middle - a reaction landing inside a shot instead of at its end. "
            "This brings that to 1, at the cost of one missed cut in 17.\n"
            "About 2 requests and 14 seconds per video. Needs an XtroEdge "
            "key; without one it does nothing.")
        self.chk_cut_check.stateChanged.connect(self._settings_changed)

        self.chk_rank = QCheckBox("Rate clips for funniness", box)
        self.chk_rank.setToolTip(
            "Rates every clip so the reaction goes on the clips worth "
            "reacting to.\nOnly used when Clip order is 'best'. About 1 "
            "request and 20 seconds per video. Needs an XtroEdge key; "
            "without one clips are taken in order.")
        self.chk_rank.stateChanged.connect(self._settings_changed)

        # The key row. It only appears while one of the AI checks is on,
        # because that is the only time it does anything - and it is the first
        # question anyone has when they tick one of those boxes. Before this
        # existed the answer was "create data\xtroedge_key.txt by hand", which
        # is not an answer.
        self.label_key = QLabel("XtroEdge key", box)
        self.edit_key = QLineEdit(box)
        self.edit_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_key.setPlaceholderText("paste the key here")
        self.edit_key.returnPressed.connect(self.save_api_key)
        self.btn_key_save = QPushButton("Save key", box)
        self.btn_key_save.clicked.connect(self.save_api_key)
        self.btn_key_test = QPushButton("Test", box)
        self.btn_key_test.setToolTip("Ask XtroEdge how much quota is left. "
                                     "Confirms the key works.")
        self.btn_key_test.clicked.connect(self.test_api_key)
        self.label_key_state = QLabel("", box)
        self.label_key_state.setWordWrap(True)
        self.label_key_state.setObjectName("keyState")

        self.edit_output = QLineEdit(box)
        self.edit_output.editingFinished.connect(self._settings_changed)
        btn_browse = QPushButton("Browse...", box)
        btn_browse.clicked.connect(self.choose_output_folder)

        self.label_estimate = QLabel("", box)
        self.label_estimate.setObjectName("hint")

        grid.addWidget(QLabel("Target length"), 0, 0)
        grid.addWidget(self.spin_target, 0, 1)
        grid.addWidget(self.chk_exact, 0, 2, 1, 2)

        frame_row = QHBoxLayout()
        frame_row.setContentsMargins(0, 0, 0, 0)
        frame_row.setSpacing(6)
        frame_row.addWidget(self.combo_ratio)
        frame_row.addWidget(self.combo_res)
        frame_row.addStretch(1)
        widget_frame = QWidget(box)
        widget_frame.setLayout(frame_row)
        grid.addWidget(QLabel("Frame"), 1, 0)
        grid.addWidget(widget_frame, 1, 1)
        grid.addWidget(QLabel("Fit source"), 1, 2)
        grid.addWidget(self.combo_fit, 1, 3)

        grid.addWidget(QLabel("Quality"), 2, 0)
        grid.addWidget(self.combo_quality, 2, 1)
        grid.addWidget(QLabel("Detector"), 2, 2)
        grid.addWidget(self.combo_detector, 2, 3)

        grid.addWidget(QLabel("Min clip length"), 3, 0)
        grid.addWidget(self.spin_min_clip, 3, 1)
        grid.addWidget(QLabel("Cut strength"), 3, 2)
        grid.addWidget(self.spin_sensitivity, 3, 3)

        grid.addWidget(QLabel("Clip order"), 4, 0)
        grid.addWidget(self.combo_selection, 4, 1)

        grid.addWidget(QLabel("AI checks"), 5, 0)
        grid.addWidget(self.chk_cut_check, 5, 1)
        grid.addWidget(self.chk_rank, 5, 2, 1, 2)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(6)
        key_row.addWidget(self.edit_key, 1)
        key_row.addWidget(self.btn_key_save)
        key_row.addWidget(self.btn_key_test)
        self.widget_key = QWidget(box)
        self.widget_key.setLayout(key_row)

        grid.addWidget(self.label_key, 6, 0)
        grid.addWidget(self.widget_key, 6, 1, 1, 3)
        grid.addWidget(self.label_key_state, 7, 1, 1, 3)

        grid.addWidget(self.label_estimate, 8, 1, 1, 3)

        grid.addWidget(QLabel("Output folder"), 9, 0)
        grid.addWidget(self.edit_output, 9, 1, 1, 2)
        grid.addWidget(btn_browse, 9, 3)
        for control in (self.spin_target, self.spin_sensitivity,
                        self.spin_min_clip, self.combo_fit, self.combo_quality,
                        self.combo_detector, self.combo_selection,
                        self.combo_ratio, self.combo_res):
            control.installEventFilter(self._wheel_guard)
            control.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(9)
        box.setSizePolicy(box.sizePolicy().horizontalPolicy(),
                          QSizePolicy.Policy.Fixed)
        return box

    # ------------------------------------------------------------------
    # settings plumbing
    # ------------------------------------------------------------------
    def _load_settings_into_ui(self) -> None:
        self._loading_settings = True
        s = self.settings
        self.spin_target.setValue(s.target_duration)
        self.chk_exact.setChecked(s.exact_duration)
        self._load_frame_combos(s.width, s.height)
        self.combo_fit.setCurrentText(s.fit_mode)
        self.combo_quality.setCurrentText(s.quality)
        self.combo_detector.setCurrentText(s.detector)
        self.spin_sensitivity.setValue(
            s.cut_strength if s.detector == "histogram"
            else s.ffmpeg_scene_threshold)
        self.spin_min_clip.setValue(s.min_clip_duration)
        self.combo_selection.setCurrentText(s.clip_selection)
        self.chk_cut_check.setChecked(s.vision_enabled)
        self.chk_rank.setChecked(s.rank_clips)
        self.edit_output.setText(s.output_dir)
        self._loading_settings = False
        self._update_ai_row()
        self._update_estimate()

    def _load_frame_combos(self, width: int, height: int) -> None:
        """Reflect width x height in the two pickers.

        A hand-edited settings.json can hold a frame these pickers do not
        offer. That must show as "custom", and stay untouched on save - the
        alternative is the window silently rewriting a deliberate custom
        frame to a preset the moment any other setting is changed.
        """
        for (ratio, res), frame in _FRAMES.items():
            if frame == (width, height):
                if self.combo_ratio.findText(_CUSTOM) >= 0:
                    self.combo_ratio.removeItem(
                        self.combo_ratio.findText(_CUSTOM))
                self.combo_ratio.setCurrentText(ratio)
                self.combo_res.setCurrentText(res)
                return
        if self.combo_ratio.findText(_CUSTOM) < 0:
            self.combo_ratio.addItem(_CUSTOM)
        self.combo_ratio.setCurrentText(_CUSTOM)

    def _settings_changed(self, *_args) -> None:
        if self._loading_settings or not self._ui_ready:
            return
        s = self.settings
        s.target_duration = self.spin_target.value()
        s.exact_duration = self.chk_exact.isChecked()
        s.fit_mode = self.combo_fit.currentText()
        s.quality = self.combo_quality.currentText()
        s.detector = self.combo_detector.currentText()
        frame = _FRAMES.get((self.combo_ratio.currentText(),
                             self.combo_res.currentText()))
        if frame is not None:
            s.width, s.height = frame
        if s.detector == "histogram":
            s.cut_strength = self.spin_sensitivity.value()
        else:
            s.ffmpeg_scene_threshold = self.spin_sensitivity.value()
        s.min_clip_duration = self.spin_min_clip.value()
        s.clip_selection = self.combo_selection.currentText()
        s.vision_enabled = self.chk_cut_check.isChecked()
        s.rank_clips = self.chk_rank.isChecked()
        folder = self.edit_output.text().strip()
        if folder:
            s.output_dir = folder
        s.save()
        self._update_ai_row()
        self._update_estimate()
        # The status bar carries both switches, so it has to follow them.
        self._refresh_status()

    def _update_ai_row(self) -> None:
        """Keep the AI switches, and the key row, honest about what they do."""
        from core import xtro
        keyed = xtro.configured()
        rating_used = (self.settings.clip_selection or "").lower() == "best"
        wanted = self.settings.vision_enabled or self.settings.rank_clips

        # The switches stay usable without a key. Disabling them was wrong:
        # you would have had to find the key first to discover the box you
        # wanted to tick, and the key field only shows once a box is ticked.
        self.chk_rank.setEnabled(rating_used)
        self.chk_cut_check.setText("Verify every cut")
        self.chk_rank.setText(
            "Rate clips for funniness" if rating_used
            else "Rate clips - needs Clip order 'best'")

        for widget in (self.label_key, self.widget_key, self.label_key_state):
            widget.setVisible(wanted)
        if not wanted:
            return

        if xtro.env_overrides():
            # Saving to the file would appear to work and change nothing.
            self.edit_key.setEnabled(False)
            self.btn_key_save.setEnabled(False)
            self.label_key_state.setText(
                f"Using the {xtro.KEY_ENV} environment variable, which takes "
                f"priority over anything saved here. Clear it to use a key "
                f"from this window.")
            self._style_key_state("ok")
            return

        self.edit_key.setEnabled(True)
        self.btn_key_save.setEnabled(True)
        if keyed:
            self.edit_key.setPlaceholderText(
                f"saved ({xtro.key_hint()}) - paste a new one to replace it")
            self.label_key_state.setText(
                f"Saved in {xtro.KEY_FILE}. Both AI checks will run.")
            self._style_key_state("ok")
        else:
            self.edit_key.setPlaceholderText("paste the key here")
            self.label_key_state.setText(
                "No key yet, so the ticked checks above will not run: clips "
                "get taken in order and cuts are judged by picture "
                "measurements alone. Paste the key and press Save key.")
            self._style_key_state("warn")

    def _style_key_state(self, kind: str) -> None:
        self.label_key_state.setProperty("state", kind)
        self.label_key_state.style().unpolish(self.label_key_state)
        self.label_key_state.style().polish(self.label_key_state)

    # ------------------------------------------------------------------
    def save_api_key(self) -> None:
        """Store what was typed, then confirm it against the API.

        Nothing here logs or displays the value - not in the status bar, not
        in a message box, not in the render log. The most it ever shows is the
        last four characters, which is enough to tell two keys apart.
        """
        from core import xtro
        typed = self.edit_key.text().strip()
        if not typed:
            QMessageBox.information(self, "No key", "Paste the key first.")
            return
        try:
            where = xtro.store_key(typed)
        except Exception as exc:
            QMessageBox.warning(self, "Key not saved", str(exc))
            return

        self.edit_key.clear()          # do not leave it sitting on screen
        # No settings.save() either: the key lives in its own file and none of
        # the settings changed, so writing them here would only risk pushing a
        # stale copy over the file.
        self._update_ai_row()
        self._refresh_status()
        self.label_status.setText(f"Key saved to {where}. Checking it...")
        self.test_api_key(quiet_on_success=True)

    def test_api_key(self, quiet_on_success: bool = False) -> None:
        """Ask the API what quota is left. The one honest test of a key."""
        from core import xtro
        if not xtro.configured():
            QMessageBox.information(self, "No key", "No key is configured yet.")
            return
        try:
            counters = xtro.usage()
        except Exception as exc:
            self.label_key_state.setText(f"The key was refused: {exc}")
            self._style_key_state("bad")
            QMessageBox.warning(self, "Key rejected", str(exc))
            return

        used = counters.get("requests_today")
        limit = counters.get("daily_request_limit")
        tokens = counters.get("tokens_today")
        token_limit = counters.get("daily_token_limit")
        left = ("unknown" if not isinstance(used, int)
                or not isinstance(limit, int) else f"{limit - used} of {limit}")
        detail = (f"Key works. Requests left today: {left}."
                  + (f" Tokens used: {tokens:,} of {token_limit:,}."
                     if isinstance(tokens, int) and isinstance(token_limit, int)
                     else ""))
        # About 3 requests a video, so turn the allowance into videos.
        if isinstance(used, int) and isinstance(limit, int):
            detail += f" Roughly {max(0, (limit - used)) // 3} more videos."
        self.label_key_state.setText(detail)
        self._style_key_state("ok")
        self.label_status.setText(detail)
        if not quiet_on_success:
            QMessageBox.information(self, "XtroEdge key", detail)

    def _detector_changed(self, name: str) -> None:
        """Each detector has its own threshold scale, so reload the value."""
        if self._loading_settings:
            return
        self.settings.detector = name
        self._loading_settings = True
        self.spin_sensitivity.setValue(
            self.settings.cut_strength if name == "histogram"
            else self.settings.ffmpeg_scene_threshold)
        self._loading_settings = False
        self._settings_changed()

    def _update_estimate(self) -> None:
        mb = ffmpeg.estimated_size_mb(self.settings)
        self.label_estimate.setText(
            f"{self.settings.resolution} MP4, about {mb:.0f} MB per video")

    def _refresh_status(self) -> None:
        try:
            encoder = ffmpeg.pick_encoder(self.settings)
        except ffmpeg.FFmpegMissing:
            encoder = "FFmpeg NOT FOUND"
        done = self.library.jobs_done_today()
        from core import xtro
        keyed = xtro.configured()
        # Every condition that actually gates the job, in the same order the
        # pipeline checks them - otherwise this line says "on" for a job that
        # will not run, which is worse than saying nothing.
        if (self.settings.clip_selection or "").lower() != "best":
            rating = "clip rating off"
        elif not self.settings.rank_clips:
            rating = "clip rating off"
        elif keyed:
            rating = "clip rating on"
        else:
            rating = "clip rating: NO KEY"
        # Worth its own indicator rather than folding into the line above:
        # this is the switch that keeps reactions out of the middle of a shot,
        # so if it is off or unkeyed the operator should be able to see it.
        if not self.settings.vision_enabled:
            cuts = "cut check OFF"
        elif keyed:
            cuts = "cut check on"
        else:
            cuts = "cut check: NO KEY"
        self.statusBar().showMessage(
            f"Encoder: {encoder}    |    Rendered today: {done}    |    "
            f"Reactions usable: {len(self.library.active_reactions())}"
            f"    |    {rating}    |    {cuts}")

    # ------------------------------------------------------------------
    # reaction library (req 12)
    # ------------------------------------------------------------------
    def refresh_reactions(self) -> None:
        self._loading_reactions = True
        self.reaction_list.clear()
        rows = self.library.all_reactions()
        for r in rows:
            text = f"{r.label}    {r.duration:.2f}s"
            if not r.exists:
                text += "   [file missing]"
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if r.active else Qt.Unchecked)
            item.setData(Qt.UserRole, r.id)
            if not r.exists:
                item.setForeground(Qt.red)
            self.reaction_list.addItem(item)
        self._loading_reactions = False
        self._refresh_reaction_summary()

    def _refresh_reaction_summary(self) -> None:
        """Labels, alarm and status bar only - never touches the list widget."""
        rows = self.library.all_reactions()
        usable = self.library.active_reactions()
        total = sum(r.duration for r in usable)
        if usable:
            shortest = min(r.duration for r in usable)
            longest = max(r.duration for r in usable)
            self.reaction_summary.setText(
                f"{len(rows)} saved, {len(usable)} in rotation. "
                f"Lengths {shortest:.2f}-{longest:.2f}s, {total:.1f}s total.")
        else:
            self.reaction_summary.setText("")

        if rows and not usable:
            self.reaction_alarm.setText(
                "Every reaction is unticked, so rendering is blocked. "
                'Press "Tick all" below.')
            self.reaction_alarm.setVisible(True)
        elif not rows:
            self.reaction_alarm.setText(
                'No reactions saved yet. Use "Add files..." below to add the '
                "short reaction clips - not the long input video.")
            self.reaction_alarm.setVisible(True)
        else:
            self.reaction_alarm.setVisible(False)
        self._refresh_status()

    def _reaction_toggled(self, item: QListWidgetItem) -> None:
        if self._loading_reactions:
            return
        rid = item.data(Qt.UserRole)
        if rid is None:
            return
        self.library.set_active(int(rid), item.checkState() == Qt.Checked)
        # Clicking the box also highlights the row. To most people the tick IS
        # the selection - the client ticked clips and pressed Remove, and Qt
        # considered nothing selected. Highlighting on tick makes the buttons
        # act on what the user just touched.
        item.setSelected(True)
        self.reaction_list.setCurrentItem(item)
        # Only the labels get refreshed here, never the list itself. Rebuilding
        # it from inside its own itemChanged signal destroys the item that is
        # still emitting, and the nested rebuild clears the loading guard early
        # - which turned a single click into the whole library being unticked.
        self._refresh_reaction_summary()

    def add_reaction_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add reaction videos", "", _MEDIA_FILTER)
        if paths:
            self._store_reactions([Path(p) for p in paths], replace=False)

    def add_reaction_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Add every video in a folder")
        if folder:
            files = _videos_in(Path(folder))
            if not files:
                QMessageBox.information(self, "Nothing found",
                                        "No video files in that folder.")
                return
            self._store_reactions(files, replace=False)

    def replace_reactions(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Replace the whole reaction library", "", _MEDIA_FILTER)
        if not paths:
            return
        if QMessageBox.question(
                self, "Replace library",
                f"Remove all {self.library.count()} saved reactions and use "
                f"these {len(paths)} instead?") != QMessageBox.Yes:
            return
        self._store_reactions([Path(p) for p in paths], replace=True)

    def _store_reactions(self, paths: list[Path], replace: bool) -> None:
        self.reaction_thread = AddReactionsWorker(paths, self.library, replace,
                                                  self)
        self.reaction_thread.status.connect(self.label_status.setText)
        self.reaction_thread.done.connect(self._reactions_stored)
        self.reaction_thread.crashed.connect(
            lambda msg: QMessageBox.critical(self, "Could not add", msg))
        self.reaction_thread.start()

    def _reactions_stored(self, count: int, errors: list) -> None:
        self.refresh_reactions()
        self.label_status.setText(f"Saved {count} reaction(s).")
        if errors:
            QMessageBox.warning(self, "Some files were skipped",
                                "\n".join(str(e) for e in errors[:12]))

    def remove_reactions(self) -> None:
        items = self.reaction_list.selectedItems()
        if not items and self.reaction_list.currentItem() is not None:
            # Ticking a box sets the current row but does not highlight it,
            # and "Remove selected" then looked dead: nothing was *selected*
            # in Qt's sense, so the old code answered "Nothing selected" to
            # someone pointing straight at a clip. Fall back to the row they
            # last touched - the confirm dialog names it, so a wrong guess
            # costs one click on No.
            items = [self.reaction_list.currentItem()]
        if not items:
            QMessageBox.information(
                self, "Nothing selected",
                "Click a clip's name to select it first (ticking the box "
                "only switches it in or out of the rotation).")
            return
        names = ", ".join(i.text().split("    ")[0] for i in items[:5])
        if len(items) > 5:
            names += ", ..."
        if QMessageBox.question(
                self, "Remove",
                f"Remove {len(items)} reaction(s) from the library?\n\n"
                f"{names}\n\nThe video files themselves are not deleted."
                ) != QMessageBox.Yes:
            return
        first_row = min(self.reaction_list.row(i) for i in items)
        for item in items:
            self.library.remove(int(item.data(Qt.UserRole)))
        self.refresh_reactions()
        # Re-select where the removed clip used to be. refresh_reactions()
        # rebuilds the list, which clears both selection and current row - so
        # the second press of Remove used to find nothing and do nothing,
        # which read as "works once, then stops". With the next row selected,
        # pressing Remove repeatedly walks down the list one confirm at a
        # time.
        remaining = self.reaction_list.count()
        if remaining:
            row = min(first_row, remaining - 1)
            self.reaction_list.setCurrentRow(row)
            self.reaction_list.item(row).setSelected(True)
        self.label_status.setText(f"Removed: {names}")

    def remove_all_reactions(self) -> None:
        """Empty the whole library in one confirmed step."""
        total = self.library.count()
        if total == 0:
            QMessageBox.information(self, "Nothing to remove",
                                    "The library is already empty.")
            return
        if QMessageBox.question(
                self, "Remove ALL",
                f"Remove all {total} reactions from the library?"
                "  The video files themselves are not deleted, so they can "
                "be added again.") != QMessageBox.Yes:
            return
        self.library.clear()
        self.refresh_reactions()
        self.label_status.setText(f"Removed all {total} reactions.")

    def set_all_active(self, active: bool) -> None:
        for r in self.library.all_reactions():
            self.library.set_active(r.id, active)
        self.refresh_reactions()
        self.label_status.setText(
            f"{'Enabled' if active else 'Disabled'} all "
            f"{self.library.count()} reactions.")

    def move_reaction(self, delta: int) -> None:
        row = self.reaction_list.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < self.reaction_list.count()):
            return
        ids = [int(self.reaction_list.item(i).data(Qt.UserRole))
               for i in range(self.reaction_list.count())]
        ids[row], ids[target] = ids[target], ids[row]
        self.library.reorder(ids)
        self.refresh_reactions()
        self.reaction_list.setCurrentRow(target)

    def purge_cache(self) -> None:
        removed = self.library.purge_cache(self.settings.render_signature())
        QMessageBox.information(
            self, "Reaction cache",
            f"Cleared {removed} cached segment(s). They will be rebuilt on the "
            "next render.")

    # ------------------------------------------------------------------
    # input queue
    # ------------------------------------------------------------------
    def queued_sources(self) -> list[Path]:
        return [Path(self.queue_list.item(i).data(Qt.UserRole))
                for i in range(self.queue_list.count())]

    def _add_sources(self, paths: list[Path]) -> None:
        """Queue long input videos, catching reaction clips added by mistake.

        The two lists are easy to confuse, and dropping a 2-second reaction in
        the input queue can only ever produce a 2-second video. Anything too
        short to fill the target is offered to the reaction library instead.
        """
        existing = {str(p) for p in self.queued_sources()}
        candidates: list[tuple[Path, float, str]] = []
        for p in paths:
            p = Path(p)
            if str(p) in existing or p.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            existing.add(str(p))
            try:
                info = ffmpeg.probe(p)
                candidates.append((p, info.usable_duration, ""))
            except Exception:
                candidates.append((p, 0.0, "unreadable"))

        target = self.settings.target_duration
        short = [c for c in candidates if c[2] or c[1] < target]
        long_enough = [c for c in candidates if c not in short]

        if short:
            names = "\n".join(f"    {p.name}  ({d:.0f}s)" for p, d, _ in short[:10])
            more = f"\n    ...and {len(short) - 10} more" if len(short) > 10 else ""
            answer = QMessageBox.question(
                self, "These look like reaction clips",
                f"{len(short)} of the files you picked are shorter than the "
                f"{target:g}s target, so they cannot be used as input videos:\n\n"
                f"{names}{more}\n\n"
                "Input videos are the long ones that get cut into clips.\n"
                "Reactions are the short ones inserted after each clip.\n\n"
                "Add these to the reaction library instead?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Yes:
                self._store_reactions([p for p, _, _ in short], replace=False)
                short = []

        for p, duration, problem in long_enough + short:
            note = f"    {duration:.0f}s" if not problem else "    [unreadable]"
            if problem or duration < target:
                note += f"  [too short for a {target:g}s video]"
            item = QListWidgetItem(f"{p.name}{note}    -  {p.parent}")
            item.setData(Qt.UserRole, str(p))
            if problem or duration < target:
                item.setForeground(Qt.red)
            self.queue_list.addItem(item)

        queued = len(long_enough) + len(short)
        if queued:
            self.label_status.setText(f"{queued} input video(s) queued.")

    def add_inputs(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose the long input video(s)", "", _MEDIA_FILTER)
        if paths:
            self._add_sources([Path(p) for p in paths])

    def add_input_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Queue a whole folder")
        if folder:
            self._add_sources(_videos_in(Path(folder)))

    def remove_inputs(self) -> None:
        for item in self.queue_list.selectedItems():
            self.queue_list.takeItem(self.queue_list.row(item))

    def clear_inputs(self) -> None:
        self.queue_list.clear()

    # drag & drop straight onto the window
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths: list[Path] = []
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                paths.extend(_videos_in(p))
            elif p.suffix.lower() in VIDEO_SUFFIXES:
                paths.append(p)
        if paths:
            self._add_sources(paths)
            event.acceptProposedAction()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def _blocking_problems(self) -> list[str]:
        return Pipeline(self.settings, self.library).preflight()

    def preview_plan(self) -> None:
        sources = self.queued_sources()
        if not sources:
            QMessageBox.information(self, "Nothing queued",
                                    "Add an input video first.")
            return
        problems = self._blocking_problems()
        if problems:
            QMessageBox.warning(self, "Not ready", "\n".join(problems))
            return

        source = sources[0]
        self.btn_preview.setEnabled(False)
        self.label_status.setText(f"Analysing {source.name}...")
        self.planner_thread = PlanWorker(source, self.settings, self.library,
                                         self)
        self.planner_thread.progress.connect(
            lambda f, ph: self.bar_current.setValue(int(f * 1000)))
        self.planner_thread.status.connect(self.label_status.setText)
        self.planner_thread.ready.connect(
            lambda plan, _clips: self._show_plan(plan, source))
        self.planner_thread.crashed.connect(self._plan_failed)
        self.planner_thread.finished.connect(
            lambda: self.btn_preview.setEnabled(True))
        self.planner_thread.start()

    def _show_plan(self, plan: RenderPlan, source: Path) -> None:
        self.bar_current.setValue(1000)
        self.label_status.setText(
            f"{plan.originals_used} clips + {plan.originals_used} reactions = "
            f"{fmt_time(plan.total_duration)}")
        PlanDialog(plan, source, self).exec()

    def _plan_failed(self, message: str) -> None:
        self.bar_current.setValue(0)
        self.label_status.setText("Preview failed.")
        QMessageBox.critical(self, "Could not plan a timeline", message)

    def start_batch(self) -> None:
        sources = self.queued_sources()
        if not sources:
            QMessageBox.information(self, "Nothing queued",
                                    "Add at least one input video.")
            return
        problems = self._blocking_problems()
        if problems:
            QMessageBox.warning(self, "Not ready", "\n".join(problems))
            return

        pipe = Pipeline(self.settings, self.library)
        warnings = pipe.warnings()
        if warnings:
            self.label_status.setText(warnings[0])

        self.results.setRowCount(0)
        self._results = []
        self.bar_current.setValue(0)
        self.bar_overall.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_preview.setEnabled(False)
        self.btn_stop.setEnabled(True)

        self.batch = BatchWorker(sources, self.settings, self.library, self)
        self.batch.progress.connect(self._on_progress)
        self.batch.status.connect(self.label_status.setText)
        self.batch.job_done.connect(self._on_job_done)
        self.batch.all_done.connect(self._on_all_done)
        self.batch.crashed.connect(self._on_crash)
        self.batch.start()

    def stop_batch(self) -> None:
        if self.batch:
            self.batch.stop()
            self.label_status.setText(
                "Stopping after the current step finishes...")
            self.btn_stop.setEnabled(False)

    def _on_progress(self, index: int, total: int, fraction: float,
                     phase: str) -> None:
        self.bar_current.setValue(int(fraction * 1000))
        overall = (index + fraction) / max(1, total)
        self.bar_overall.setValue(int(overall * 1000))
        self.bar_current.setFormat(f"{phase}  %p%")

    def _on_job_done(self, index: int, result: JobResult) -> None:
        self._results.append(result)
        row = self.results.rowCount()
        self.results.insertRow(row)

        report = result.validation
        if result.ok and report:
            verdict = "OK"
            length = fmt_time(report.duration)
            size = f"{report.size_mb:.1f} MB"
        elif report:
            verdict = "Check: " + "; ".join(report.problems)
            length = fmt_time(report.duration)
            size = f"{report.size_mb:.1f} MB"
        else:
            verdict = result.error or "Failed"
            length = size = "-"

        cells = [
            result.source.name,
            str(result.output) if result.output else "-",
            length, size, f"{result.elapsed:.0f}s", verdict,
        ]
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if col == 1 and result.output:
                item.setData(Qt.UserRole, str(result.output))
            if not result.ok:
                item.setForeground(Qt.darkRed)
            self.results.setItem(row, col, item)
        self.results.resizeColumnsToContents()
        self.results.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self._refresh_status()

    def _on_all_done(self, results: list) -> None:
        self.btn_start.setEnabled(True)
        self.btn_preview.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.bar_current.setFormat("%p%")
        ok = sum(1 for r in results if r.ok)
        self.label_status.setText(
            f"Finished: {ok} of {len(results)} rendered and validated.")
        self._refresh_status()

    def _on_crash(self, message: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_preview.setEnabled(True)
        self.btn_stop.setEnabled(False)
        QMessageBox.critical(self, "Rendering stopped", message)

    # ------------------------------------------------------------------
    # misc actions
    # ------------------------------------------------------------------
    def choose_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Where should finished videos go?", self.settings.output_dir)
        if folder:
            self.edit_output.setText(folder)
            self._settings_changed()

    def open_output_folder(self) -> None:
        folder = Path(self.settings.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _open_result_row(self, index) -> None:
        item = self.results.item(index.row(), 1)
        if item and item.data(Qt.UserRole):
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(item.data(Qt.UserRole))))

    def show_history(self) -> None:
        jobs = self.library.recent_jobs(60)
        lines = [f"Rendered today: {self.library.jobs_done_today()}", ""]
        for j in jobs:
            mark = "OK  " if j["ok"] else "FAIL"
            lines.append(
                f"{mark} {j['created_at']}  {Path(j['source']).name}  "
                f"{j['duration']:.1f}s  {j['size_bytes'] / 1048576:.1f} MB  "
                f"{j['elapsed']:.0f}s"
                + (f"  {j['error']}" if j["error"] else ""))
        self._show_text("Render history", "\n".join(lines) or "Nothing yet.")

    def show_doctor(self) -> None:
        lines = [f"{k}: {v}" for k, v in ffmpeg.tool_versions().items()]
        try:
            lines.append(f"encoders available: "
                         f"{', '.join(ffmpeg.available_encoders())}")
            lines.append(f"encoder selected: "
                         f"{ffmpeg.pick_encoder(self.settings)}")
        except ffmpeg.FFmpegMissing as exc:
            lines.append(str(exc))
        for module, label in (("scenedetect", "PySceneDetect"),
                              ("cv2", "OpenCV"), ("PySide6", "PySide6")):
            try:
                mod = __import__(module)
                lines.append(f"{label}: {getattr(mod, '__version__', 'yes')}")
            except ImportError:
                lines.append(f"{label}: not installed")
        from core import xtro
        lines.append("")
        lines.append(f"XtroEdge key: {xtro.key_source()}")
        if xtro.configured():
            try:
                counters = xtro.usage()
                lines.append(
                    f"XtroEdge today: {counters.get('requests_today')}/"
                    f"{counters.get('daily_request_limit')} requests, "
                    f"{counters.get('tokens_today')}/"
                    f"{counters.get('daily_token_limit')} tokens")
            except Exception as exc:
                lines.append(f"XtroEdge usage unavailable: {exc}")

        pipe = Pipeline(self.settings, self.library)
        lines.append("")
        lines.extend(f"warning: {w}" for w in pipe.warnings())
        problems = pipe.preflight()
        lines.append("")
        lines.append("READY" if not problems
                     else "NOT READY:\n" + "\n".join(f"  - {p}"
                                                     for p in problems))
        self._show_text("Environment check", "\n".join(lines))

    def _show_text(self, title: str, body: str) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(760, 520)
        layout = QVBoxLayout(dlg)
        view = QTextEdit(dlg)
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 9))
        view.setPlainText(body)
        layout.addWidget(view)
        btn = QPushButton("Close", dlg)
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn, alignment=Qt.AlignRight)
        dlg.exec()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.batch and self.batch.isRunning():
            if QMessageBox.question(
                    self, "Still rendering",
                    "A render is running. Stop it and quit?") != QMessageBox.Yes:
                event.ignore()
                return
            self.batch.stop()
            self.batch.wait(8000)
        # Deliberately no settings.save() here. Every setting is written the
        # moment it changes, so a second write on close adds nothing - and it
        # is exactly how a stale in-memory copy overwrites newer values that
        # something else wrote in the meantime. Three times in this session a
        # setting turned itself off that way: the CLI or a second window
        # updated the file, then a window that had loaded the old values
        # closed and put them back.
        event.accept()


def _videos_in(folder: Path) -> list[Path]:
    return sorted(f for f in folder.iterdir()
                  if f.is_file() and f.suffix.lower() in VIDEO_SUFFIXES)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Reaction Video Builder")
    theme.apply(app)

    # One window at a time. Two of them share one settings.json, one
    # library.db and one output folder, and the second to close overwrites
    # whatever the first had - which looks exactly like the app forgetting a
    # setting on its own. The lock is held for the life of the process and
    # released by the OS if it is killed.
    # ensure_dirs() must run BEFORE the lock: the lock file lives under
    # data/, and on a fresh install data/ does not exist yet. QLockFile
    # cannot create a file in a missing directory, and that failure is
    # indistinguishable from "someone holds the lock" - so the very first
    # launch on a clean machine said 'Already running' with nothing running,
    # and the app could never be started at all.
    ensure_dirs()
    lock = QLockFile(str(DATA_DIR / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        if lock.error() == QLockFile.LockError.LockFailedError:
            QMessageBox.information(
                None, "Already running",
                "Reaction Video Builder is already open.\n\nTwo windows "
                "would share the same reaction library and settings and "
                "overwrite each other, so this one will close. Switch to "
                "the window that is already open.")
            return 0
        # The lock could not be created at all (permissions, an odd
        # filesystem). Running without the one-window guard beats a tool
        # that refuses to start.

    window = MainWindow()
    window.show()
    try:
        return app.exec()
    finally:
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
