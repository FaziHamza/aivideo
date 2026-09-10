"""XtroEdge branding for the window, kept out of the app logic.

Why a stylesheet rather than restyling widgets in code: the app builds its
panels once and this file can be rewritten without touching any of that, so a
look change cannot break a signal connection.

The palette is the official brand set, not an interpretation of it:

    primary green   #4ADE80 -> #16A34A   (the gradient, light to deep)
    charcoal text   #374151
    white text      #FFFFFF
    tagline gray    #6B7280

Two decisions worth recording, because both are easy to undo by accident.

**The header is white, not green.** The official logo has a charcoal
wordmark: it reads on a light surface and disappears on green. A green header
was tried first with the white-wordmark variant, and it works, but it means
showing a different logo from the one on the website. White header, real logo,
and the green moved to the rule beneath it - which is also more honest to
"light theme".

**Where white text sits on green it is flat #16A34A**, never #4ADE80. The
light end of the gradient cannot carry white type. It is used for the sweep in
the header rule and the progress chunk, and for hover tints.

**The greys are the brand greys, extended rather than replaced.** #374151 and
#6B7280 are Tailwind's gray-700 and gray-500, so borders and backgrounds here
use the rest of that ramp (gray-200 for lines, gray-50 for the window). Any
other grey would read as a second, slightly-off palette.

`state` is a dynamic Qt property, so a label can be repainted as ok/warn/bad
by setting the property and re-polishing - see `_style_key_state` in app.py.
"""

from __future__ import annotations

from pathlib import Path

from core.config import BUNDLE_DIR, PROJECT_ROOT

# --- brand ----------------------------------------------------------------
GREEN_LIGHT = "#4ADE80"      # the light end of the brand gradient
GREEN = "#16A34A"            # the deep end: the only green white type sits on
GREEN_DEEP = "#128A3E"       # hover/pressed, one step darker than GREEN
GREEN_SOFT = "#E7F8ED"       # selection and hover tints
GREEN_EDGE = "#BBEBCC"       # a border that reads as green without shouting
TEXT = "#374151"             # charcoal
WHITE = "#FFFFFF"
MUTED = "#6B7280"            # tagline gray

# --- neutrals, from the same ramp as the brand greys ----------------------
BG = "#F9FAFB"
SURFACE = WHITE
LINE = "#E5E7EB"
LINE_STRONG = "#D1D5DB"
DISABLED_BG = "#F3F4F6"
DISABLED_TEXT = "#9CA3AF"

# --- outcomes -------------------------------------------------------------
OK = GREEN                   # a pass is on-brand by definition
WARN = "#B45309"
BAD = "#B91C1C"

# The official logo with the charcoal wordmark - the light-background version.
# There is a white-wordmark variant on the same server for dark backgrounds;
# it is kept beside this one but not used, since the app is a light theme.
LOGO_NAME = "xtroedge-logo-colour.png"
LOGO_ON_DARK_NAME = "xtroedge-logo-white.png"
# The X mark on its own. The wordmark is unreadable at 16px, so the icon uses
# the symbol.
SYMBOL_NAME = "xtroedge-symbol.png"
ICON_NAME = "app-icon.png"


def _asset(name: str) -> Path | None:
    """An asset, wherever it ended up.

    Checked in three places because assets live in `assets/` in the source
    tree, inside the PyInstaller payload in a build, and beside the .exe if
    someone drops a replacement there - and the last of those should win, so
    the branding can be changed without a rebuild.
    """
    for root in (PROJECT_ROOT / "assets", BUNDLE_DIR / "assets",
                 Path(__file__).resolve().parent.parent / "assets"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def icon_path() -> Path | None:
    """The window and taskbar icon."""
    return _asset(ICON_NAME)


def logo_path() -> Path | None:
    """The brand logo for the header bar."""
    return _asset(LOGO_NAME)


def symbol_path() -> Path | None:
    """The X mark on its own, for icons and anywhere small."""
    return _asset(SYMBOL_NAME)


STYLESHEET = f"""
/* ---------- base ---------- */
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
    font-size: 9pt;
}}
QMainWindow, QDialog {{ background: {BG}; }}

/* ---------- brand header ---------- */
/* White, not green. The official logo has a charcoal wordmark, which is
   what makes it legible on a light surface and illegible on green - and the
   app is a light theme, so the logo picks the header rather than the other
   way round. The brand green stays present as the rule underneath it and on
   every control that matters. */
QWidget#brandHeader {{
    background: {SURFACE};
    border: none;
    border-bottom: 1px solid {LINE};
}}
QWidget#brandHeader QLabel {{ background: transparent; }}
QWidget#brandRule {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {GREEN}, stop:1 {GREEN_LIGHT});
    border: none;
}}
QLabel#brandTitle {{
    color: {TEXT};
    font-size: 12pt;
    font-weight: 600;
}}
QLabel#brandTagline {{
    color: {MUTED};
    font-size: 8.5pt;
}}

/* ---------- panels ---------- */
QGroupBox {{
    background: {SURFACE};
    border: 1px solid {LINE};
    border-radius: 10px;
    margin-top: 16px;
    padding: 14px 14px 12px 14px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: {MUTED};
    font-size: 8.5pt;
    font-weight: 700;
    text-transform: uppercase;
}}

/* ---------- text ---------- */
QLabel {{ background: transparent; }}
QLabel#hint {{ color: {MUTED}; }}

/* Outcome labels. Set the `state` property, then re-polish. */
QLabel#keyState {{
    color: {MUTED};
    background: transparent;
    padding: 2px 0;
}}
QLabel#keyState[state="ok"] {{ color: {OK}; font-weight: 600; }}
QLabel#keyState[state="warn"] {{ color: {WARN}; font-weight: 600; }}
QLabel#keyState[state="bad"] {{ color: {BAD}; font-weight: 600; }}

/* ---------- inputs ---------- */
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QTextEdit {{
    background: {SURFACE};
    border: 1px solid {LINE_STRONG};
    border-radius: 7px;
    padding: 6px 9px;
    selection-background-color: {GREEN};
    selection-color: {WHITE};
    min-height: 20px;
}}
QLineEdit:hover, QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover {{
    border-color: {MUTED};
}}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus,
QTextEdit:focus {{
    border-color: {GREEN};
}}
QLineEdit:disabled, QComboBox:disabled, QDoubleSpinBox:disabled {{
    background: {DISABLED_BG};
    color: {DISABLED_TEXT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {MUTED};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    border: 1px solid {LINE};
    border-radius: 7px;
    padding: 4px;
    selection-background-color: {GREEN_SOFT};
    selection-color: {TEXT};
    outline: none;
}}
QDoubleSpinBox::up-button, QSpinBox::up-button,
QDoubleSpinBox::down-button, QSpinBox::down-button {{
    background: transparent;
    border: none;
    width: 16px;
}}

/* ---------- buttons ---------- */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {LINE_STRONG};
    border-radius: 7px;
    padding: 6px 14px;
    min-height: 20px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {GREEN_SOFT};
    border-color: {GREEN_EDGE};
    color: {GREEN_DEEP};
}}
QPushButton:pressed {{ background: #D8F2E2; }}
QPushButton:disabled {{
    background: {DISABLED_BG};
    color: {DISABLED_TEXT};
    border-color: {LINE};
}}
/* The one primary action on the screen. Flat deep green, not the gradient:
   white type needs the contrast. */
QPushButton#primary {{
    background: {GREEN};
    border: 1px solid {GREEN};
    color: {WHITE};
    font-weight: 600;
    padding: 7px 20px;
}}
QPushButton#primary:hover {{
    background: {GREEN_DEEP}; border-color: {GREEN_DEEP}; color: {WHITE};
}}
QPushButton#primary:pressed {{ background: #0F7434; }}
QPushButton#primary:disabled {{
    background: #A7DCBB; border-color: #A7DCBB; color: #F2FBF5;
}}
QPushButton#danger {{ color: {BAD}; }}
QPushButton#danger:hover {{
    background: #FEF2F2; border-color: #FCA5A5; color: {BAD};
}}

/* ---------- checkboxes ---------- */
QCheckBox {{ background: transparent; spacing: 8px; padding: 2px 0; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {LINE_STRONG};
    border-radius: 4px;
    background: {SURFACE};
}}
QCheckBox::indicator:hover {{ border-color: {GREEN}; }}
QCheckBox::indicator:checked {{
    background: {GREEN};
    border-color: {GREEN};
    image: none;
}}
QCheckBox::indicator:disabled {{ background: {DISABLED_BG}; border-color: {LINE}; }}
QCheckBox:disabled {{ color: {DISABLED_TEXT}; }}

/* ---------- lists and tables ---------- */
QListWidget, QTableWidget {{
    background: {SURFACE};
    border: 1px solid {LINE};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item, QTableWidget::item {{
    padding: 5px 7px;
    border-bottom: 1px solid #F3F4F6;
}}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: {GREEN_SOFT};
    color: {TEXT};
}}
QListWidget::item:hover, QTableWidget::item:hover {{ background: #FAFBFA; }}
QHeaderView::section {{
    background: #FAFBFB;
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {LINE};
    padding: 7px 8px;
    font-size: 8.5pt;
    font-weight: 700;
}}
QTableWidget {{ gridline-color: transparent; }}

/* ---------- progress ---------- */
QProgressBar {{
    background: {DISABLED_BG};
    border: none;
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}}
/* Progress is the one other place the gradient earns its keep: it reads as
   movement across the bar rather than a flat block. */
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {GREEN_LIGHT}, stop:1 {GREEN});
    border-radius: 5px;
}}

/* ---------- chrome ---------- */
QSplitter::handle {{ background: transparent; width: 10px; }}
QMenuBar {{ background: {SURFACE}; border-bottom: 1px solid {LINE}; }}
QMenuBar::item {{ padding: 6px 11px; background: transparent; }}
QMenuBar::item:selected {{ background: {GREEN_SOFT}; border-radius: 5px; }}
QMenu {{
    background: {SURFACE};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background: {GREEN_SOFT}; }}
QStatusBar {{
    background: {SURFACE};
    border-top: 1px solid {LINE};
    color: {MUTED};
}}
QStatusBar::item {{ border: none; }}
QToolTip {{
    background: {TEXT};
    color: {WHITE};
    border: none;
    border-radius: 6px;
    padding: 7px 9px;
}}

/* ---------- scrollbars ---------- */
QScrollArea {{ background: {BG}; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {LINE_STRONG}; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {MUTED}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {LINE_STRONG}; border-radius: 5px; min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: {MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def apply(app) -> None:
    """Style a QApplication. Safe to call before the window exists."""
    app.setStyle("Fusion")   # a predictable base for the sheet to sit on
    app.setStyleSheet(STYLESHEET)
