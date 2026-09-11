"""UI-independent engine for the reaction-video tool.

Everything in this package is pure Python with no Qt / no web imports, so the
same pipeline backs the desktop app today and a web frontend later.
"""

# One version for the whole product, read by the window title, the CLI, the
# environment check and both PyInstaller specs. It lives here rather than in
# a shell because the engine is the thing being versioned and both shells are
# thin over it.
#
# Bump the middle number for a change someone would notice and the last one
# for a fix. `_VERSION_TUPLE` exists because a Windows version resource needs
# four integers, and a mismatch between the two is the sort of thing nobody
# finds until support asks a customer which build they are on.
__version__ = "1.0.0"
_VERSION_TUPLE = tuple(int(p) for p in __version__.split(".")) + (0,)
