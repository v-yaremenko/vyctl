"""
Clipboard access for vyctl.

vyctl *is* the terminal emulator around each Claude session, so copy and paste are
its job rather than the host terminal's.  Inside a TUI there is no portable way to read
the clipboard (OSC 52 is write-only in practice), but this app is Windows-only, so the
Win32 clipboard is right there.

* :func:`read_text` uses ``user32``/``kernel32`` directly -- no process spawn, so paste
  feels instant.
* :func:`write_text` shells out to ``Set-Clipboard``, passing the text on stdin so no
  quoting can mangle it.  Copying is rare enough that the process cost does not matter,
  and it avoids the much longer ctypes dance that writing requires.
"""

from __future__ import annotations

import subprocess
import sys

from . import logs

#: Suppress the console window that would otherwise flash.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def read_text() -> str:
    """Return the clipboard's text, or "" when it holds nothing usable."""
    if sys.platform != "win32":  # pragma: no cover - vyctl targets Windows
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]

        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.c_wchar_p(pointer).value or ""
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:
        logs.log().exception("clipboard read failed")
        return ""


def write_text(text: str) -> bool:
    """Put *text* on the clipboard.  Returns True on success."""
    if not text or sys.platform != "win32":
        return False
    try:
        # -NoProfile keeps it fast; the text goes via stdin so quoting is a non-issue.
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$input | Set-Clipboard",
            ],
            input=text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=10,
            check=False,
        )
        return completed.returncode == 0
    except Exception:
        logs.log().exception("clipboard write failed")
        return False
