"""Builds the Windows version resource that both PyInstaller specs embed.

Why generated rather than a checked-in text file: a Windows version resource
wants the number four times over, twice as a tuple of integers and twice as a
string, and a hand-maintained copy of it drifts from `core.__version__` the
first time anyone bumps a version in a hurry. Support then asks a customer
which build they are on and gets an answer that is not true.

The exe's Properties > Details tab reads this. An unsigned build with blank
metadata is also the shape antivirus heuristics like least, so filling it in
costs nothing and helps a little.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import __version__, _VERSION_TUPLE  # noqa: E402

COMPANY = "XtroEdge"
PRODUCT = "Reaction Video Builder"
DESCRIPTION = "One long video in, a 3-minute reaction cut out"

_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vers},
    prodvers={vers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', '{company}'),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', '{product}'),
         StringStruct('OriginalFilename', 'ReactionVideoBuilder.exe'),
         StringStruct('ProductName', '{product}'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def write(target: Path) -> Path:
    """Write the resource file next to the specs and return its path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _TEMPLATE.format(vers=_VERSION_TUPLE, version=__version__,
                         company=COMPANY, product=PRODUCT,
                         description=DESCRIPTION),
        encoding="utf-8")
    return target


if __name__ == "__main__":
    out = write(Path(__file__).resolve().parent / "version_info.txt")
    print(f"wrote {out}")
    print(out.read_text(encoding="utf-8"))
