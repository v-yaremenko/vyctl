"""
Background process control for vyctl.

Each configured project owns one :class:`ClaudeSession`, which is a persistent
``powershell.exe`` instance running in that project's directory with its stdin, stdout
and stderr attached to pipes.  Prompts typed in the dashboard are written to the shell's
stdin; output is read back with non-blocking async reads and pushed into the UI through
callbacks.

Why prompts are dispatched as ``claude --print`` invocations
-----------------------------------------------------------
``claude`` with no arguments is a full-screen interactive TUI: it requires a real console
(a TTY/ConPTY) and takes over the terminal with cursor addressing and raw-mode key
handling.  When its stdio are anonymous pipes -- which is exactly what a wrapper like
vyctl needs in order to render output inside its own panes -- the interactive UI
cannot run, and its escape sequences would be unreadable garbage even if it did.

So the persistent-PowerShell architecture is kept as specified, but each prompt is sent
to the live shell as a *headless* Claude turn:

    claude --resume <session-id> --print --output-format stream-json --verbose '<prompt>'

That is pipe-friendly, streams structured events we can render live, and -- thanks to
``--continue`` / ``--resume`` -- keeps one continuous conversation per project, which is
the actual goal.  The shell itself stays alive across prompts, so environment changes,
``Set-Location``, and anything else you type into a pane persist between turns.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from enum import Enum
from pathlib import Path

from .config import AppConfig, Project

# --------------------------------------------------------------------------------------
# Windows helpers
# --------------------------------------------------------------------------------------

#: Suppress the console window that would otherwise flash for every child process.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

#: PIDs of shells we have spawned.  Used by the ``atexit`` safety net below so that even
#: an abrupt interpreter shutdown cannot leave orphaned powershell/claude processes.
_LIVE_PIDS: set[int] = set()


def powershell_executable() -> str:
    """Locate a PowerShell binary, preferring Windows PowerShell as specified."""
    for candidate in ("powershell", "pwsh"):
        found = shutil.which(candidate)
        if found:
            return found
    # Fall back to the canonical absolute path (PATH can be unusual in some shells).
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    fallback = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(fallback)


class ProcessJob:
    """A Windows *job object* that owns one shell and everything it spawns.

    ``taskkill /T`` walks parent->child links, which is not enough on its own: once the
    shell exits, anything it started (the ``claude`` node process, or a server started
    with ``Start-Process``) is reparented and becomes unreachable -- exactly the orphan
    situation we must avoid.  A job object is authoritative instead: every descendant
    inherits membership, and ``TerminateJobObject`` kills the whole set at once no matter
    what the process tree looks like by then.

    Falls back to a no-op (leaving ``kill_process_tree`` to do the work) if any of the
    Win32 calls are unavailable.
    """

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFO = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    def __init__(self) -> None:
        self._handle = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return

            class _BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )]

            class _ExtendedLimits(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = _ExtendedLimits()
            # Kill every member the moment the last handle to the job closes -- this is
            # what protects us even if vyctl is killed outright.
            info.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFO,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(handle)
                return

            self._kernel32 = kernel32
            self._handle = handle
        except Exception:  # pragma: no cover - any Win32 hiccup -> graceful fallback
            self._handle = None

    def assign(self, pid: int) -> bool:
        """Put *pid* (and therefore all its future descendants) into the job."""
        if self._handle is None:
            return False
        try:
            access = self._PROCESS_TERMINATE | self._PROCESS_SET_QUOTA
            process = self._kernel32.OpenProcess(access, False, pid)
            if not process:
                return False
            try:
                return bool(self._kernel32.AssignProcessToJobObject(self._handle, process))
            finally:
                self._kernel32.CloseHandle(process)
        except Exception:  # pragma: no cover
            return False

    def terminate(self) -> None:
        """Kill every process in the job, then release the handle."""
        if self._handle is None:
            return
        try:
            self._kernel32.TerminateJobObject(self._handle, 1)
        except Exception:  # pragma: no cover
            pass
        self.close()

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            self._kernel32.CloseHandle(self._handle)
        except Exception:  # pragma: no cover
            pass
        self._handle = None


def kill_process_tree(pid: int) -> None:
    """Forcefully kill *pid* and every descendant.

    ``taskkill /T`` is the reliable way to reach grandchildren on Windows: killing the
    shell alone would leave the ``claude`` (Node) process it spawned running.
    """
    if pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:  # pragma: no cover - vyctl targets Windows, but stay portable
        try:
            os.killpg(os.getpgid(pid), 9)
        except (OSError, ProcessLookupError):
            pass
    _LIVE_PIDS.discard(pid)


@atexit.register
def _kill_orphans() -> None:
    """Last-resort teardown: runs even if the app dies without unmounting cleanly."""
    for pid in list(_LIVE_PIDS):
        kill_process_tree(pid)


# --------------------------------------------------------------------------------------
# Status + output plumbing
# --------------------------------------------------------------------------------------


class SessionStatus(str, Enum):
    """Lifecycle state of a session, rendered as the pane's colour-coded badge."""

    INACTIVE = "inactive"  # no process running
    STARTING = "starting"  # shell spawned, bootstrap in flight
    IDLE = "idle"  # shell alive, no Claude turn in flight
    WORKING = "working"  # a Claude turn is running
    ERROR = "error"  # shell died unexpectedly / last turn failed

    @property
    def label(self) -> str:
        return {
            SessionStatus.INACTIVE: "Inactive",
            SessionStatus.STARTING: "Starting",
            SessionStatus.IDLE: "Idle",
            SessionStatus.WORKING: "Thinking/Working",
            SessionStatus.ERROR: "Error",
        }[self]


#: Output classification.  The UI maps these to colours; keeping the process layer free of
#: Rich/Textual imports makes it independently testable.
Kind = str
KINDS = (
    "info",  # vyctl's own commentary
    "assistant",  # Claude's prose
    "thinking",  # extended-thinking blocks
    "tool",  # tool invocations
    "tool_result",  # tool results
    "result",  # end-of-turn summary (cost/duration)
    "stdout",  # raw shell stdout
    "stderr",  # raw shell stderr
    "error",  # failures
)

#: ``(session, kind, text)`` -> None
OutputCallback = Callable[["ClaudeSession", Kind, str], None]
#: ``(session, status)`` -> None
StatusCallback = Callable[["ClaudeSession", SessionStatus], None]


def ps_quote(value: str) -> str:
    """Quote *value* as a PowerShell single-quoted string literal.

    Inside single quotes PowerShell treats everything literally; the only escape needed
    is doubling an embedded single quote.  This is what keeps a prompt containing
    ``$env:PATH``, backticks, semicolons or quotes from being reinterpreted as code.
    """
    return "'" + value.replace("'", "''") + "'"


def _flatten(prompt: str) -> str:
    """Collapse a prompt to one line -- the shell's stdin is line oriented."""
    return " ".join(prompt.replace("\r", "").split("\n")).strip()


def _summarise_tool_input(name: str, payload: dict) -> str:
    """Produce a short, useful one-liner for a tool_use block."""
    if not isinstance(payload, dict):
        return ""
    for key in ("file_path", "path", "command", "pattern", "query", "url", "prompt", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            single = " ".join(value.split())
            return single if len(single) <= 110 else single[:107] + "..."
    return ""


# --------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------


class ClaudeSession:
    """A persistent background PowerShell running Claude Code for one project."""

    #: How long to wait for a graceful ``exit`` before force-killing the tree.
    GRACE_SECONDS = 1.5

    def __init__(
        self,
        project: Project,
        config: AppConfig,
        on_output: OutputCallback,
        on_status: StatusCallback,
    ) -> None:
        self.project = project
        self.config = config
        self._on_output = on_output
        self._on_status = on_status

        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._status = SessionStatus.INACTIVE
        self._write_lock = asyncio.Lock()
        #: Job object owning this shell and all of its descendants (see ProcessJob).
        self._job: ProcessJob | None = None

        #: Claude session id, learned from the stream-json ``init``/``result`` events and
        #: then used with ``--resume`` so every prompt lands in the same conversation.
        self.session_id: str | None = None
        #: Arm ``--continue`` for the first prompt (auto-resume).
        self._continue_next = bool(config.auto_resume)
        #: Per-session random token so shell output can never accidentally spoof it.
        self._sentinel = f"<<AO-{uuid.uuid4().hex[:10]}-DONE>>"
        #: Retry bookkeeping for the "no previous conversation" fallback.
        self._last_prompt: str | None = None
        self._retried_without_continue = False
        #: Rolling per-turn stats surfaced in the pane header.
        self.last_cost_usd: float = 0.0
        self.turns: int = 0

    # -- introspection ----------------------------------------------------------

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc and self._proc.returncode is None else None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def is_busy(self) -> bool:
        return self._status is SessionStatus.WORKING

    # -- output/status helpers --------------------------------------------------

    def _emit(self, kind: Kind, text: str) -> None:
        """Push a line to the UI, swallowing UI errors so streaming never dies."""
        try:
            self._on_output(self, kind, text)
        except Exception:  # pragma: no cover - a broken pane must not kill the reader
            pass

    def _set_status(self, status: SessionStatus) -> None:
        if status is self._status:
            return
        self._status = status
        try:
            self._on_status(self, status)
        except Exception:  # pragma: no cover
            pass

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> bool:
        """Spawn the background shell.  Returns True when the process is live."""
        if self.is_running:
            return True

        work_dir = self.project.resolved_path
        if not self.project.path_exists():
            self._emit("error", f"folder not found: {work_dir}")
            self._set_status(SessionStatus.ERROR)
            return False

        self._set_status(SessionStatus.STARTING)
        shell = powershell_executable()
        try:
            # '-Command -' makes PowerShell read and execute commands from stdin, which
            # is what turns it into a persistent, scriptable background shell.
            self._proc = await asyncio.create_subprocess_exec(
                shell,
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "-",
                cwd=str(work_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except (OSError, NotImplementedError) as exc:
            self._emit("error", f"could not start PowerShell: {exc}")
            self._set_status(SessionStatus.ERROR)
            return False

        _LIVE_PIDS.add(self._proc.pid)

        # Capture the shell in a job object so that no descendant -- the claude/node
        # process, or anything it detaches -- can outlive this session.
        self._job = ProcessJob()
        job_ok = self._job.assign(self._proc.pid)
        if not job_ok:
            self._job.close()
            self._job = None

        self._emit(
            "info",
            f"powershell pid {self._proc.pid} in {work_dir}"
            + ("" if job_ok else " (job object unavailable; using taskkill on teardown)"),
        )

        # One reader task per stream, plus a watchdog for unexpected shell death.
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._pump(self._proc.stdout, "stdout")),
            loop.create_task(self._pump(self._proc.stderr, "stderr")),
            loop.create_task(self._watch_exit()),
        ]

        await self._bootstrap()
        return True

    async def _bootstrap(self) -> None:
        """Configure the fresh shell (UTF-8 output, quiet progress) and report ready."""
        await self._write_lines(
            [
                "$ErrorActionPreference = 'Continue'",
                "$ProgressPreference = 'SilentlyContinue'",
                # Without this, Claude's box-drawing/emoji output arrives mojibaked.
                "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}",
                "$env:PYTHONIOENCODING = 'utf-8'",
                # Claude Code's own spinner/ANSI output is pointless in a pipe.
                "$env:FORCE_COLOR = '0'",
                "$env:TERM = 'dumb'",
            ]
        )
        self._set_status(SessionStatus.IDLE)
        if self.config.auto_resume:
            self._emit(
                "info",
                "auto-resume armed: the next prompt continues this folder's latest "
                "Claude conversation (--continue)",
            )

    async def _watch_exit(self) -> None:
        """Note when the shell exits so the badge can flip to Inactive/Error."""
        proc = self._proc
        if proc is None:
            return
        code = await proc.wait()
        _LIVE_PIDS.discard(proc.pid)
        if self._status is not SessionStatus.INACTIVE:  # not a deliberate stop()
            self._emit("info" if code == 0 else "error", f"shell exited (code {code})")
            self._set_status(SessionStatus.INACTIVE if code == 0 else SessionStatus.ERROR)

    async def stop(self) -> None:
        """Gracefully stop the shell, then force-kill the whole tree."""
        proc, self._proc = self._proc, None
        job, self._job = self._job, None
        self._set_status(SessionStatus.INACTIVE)

        if proc is not None and proc.returncode is None:
            pid = proc.pid
            loop = asyncio.get_running_loop()
            # Ask politely first: 'exit' lets PowerShell run its own cleanup.
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.write(b"exit\n")
                    await proc.stdin.drain()
                    proc.stdin.close()
            except (OSError, ConnectionError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.GRACE_SECONDS)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

            # Then make sure.  The job object is the reliable step: by now the shell may
            # already be gone, which breaks the parent->child links taskkill relies on,
            # but job membership still covers every descendant.
            if job is not None:
                await loop.run_in_executor(None, job.terminate)
            await loop.run_in_executor(None, kill_process_tree, pid)
        elif job is not None:
            await asyncio.get_running_loop().run_in_executor(None, job.terminate)

        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def restart(self) -> bool:
        """Stop and start again -- also the way to abort a runaway Claude turn."""
        await self.stop()
        self.session_id = None
        self._continue_next = bool(self.config.auto_resume)
        self._retried_without_continue = False
        return await self.start()

    # -- writing ----------------------------------------------------------------

    async def _write_lines(self, lines: list[str]) -> bool:
        """Write command lines to the shell's stdin (serialised by a lock)."""
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            self._emit("error", "session is not running")
            return False
        payload = "".join(line + "\n" for line in lines).encode("utf-8", "replace")
        async with self._write_lock:
            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
            except (OSError, ConnectionError, RuntimeError) as exc:
                self._emit("error", f"write failed: {exc}")
                self._set_status(SessionStatus.ERROR)
                return False
        return True

    def _build_claude_command(self, prompt: str) -> str:
        """Assemble the headless Claude invocation for one prompt."""
        base = (self.project.command or "claude").strip() or "claude"
        parts = [base]

        # Session continuity: prefer an explicit id, fall back to --continue.
        if self.session_id:
            parts += ["--resume", self.session_id]
        elif self._continue_next:
            parts.append("--continue")

        parts.append("--print")
        if self.config.stream_json:
            # stream-json gives incremental, structured events; --verbose is required
            # by the CLI when combining --print with stream-json.
            parts += ["--output-format", "stream-json", "--verbose"]

        extra = (self.project.extra_args or "").strip()
        if extra:
            parts.append(extra)

        parts.append(ps_quote(prompt))
        return " ".join(parts)

    async def send_prompt(self, prompt: str) -> bool:
        """Send *prompt* to Claude inside this project's shell."""
        text = _flatten(prompt)
        if not text:
            return False
        if not self.is_running and not await self.start():
            return False
        if self.is_busy:
            self._emit("error", "a turn is already running -- wait for it to finish")
            return False

        self._last_prompt = text
        self._retried_without_continue = False
        return await self._dispatch(text)

    async def _dispatch(self, text: str) -> bool:
        """Write one Claude turn plus its completion sentinel."""
        command = self._build_claude_command(text)
        self._emit("info", f"> {text}")
        self._set_status(SessionStatus.WORKING)
        # The sentinel is echoed by the shell once the turn's process has exited, which is
        # how we detect completion even when Claude produces no parsable result event.
        ok = await self._write_lines([f"{command}; Write-Output {ps_quote(self._sentinel)}"])
        if not ok:
            self._set_status(SessionStatus.ERROR)
        return ok

    async def send_shell_line(self, line: str) -> bool:
        """Send a raw PowerShell line (pane input prefixed with ``!``)."""
        text = _flatten(line)
        if not text:
            return False
        if not self.is_running and not await self.start():
            return False
        self._emit("info", f"PS> {text}")
        self._set_status(SessionStatus.WORKING)
        return await self._write_lines([f"{text}; Write-Output {ps_quote(self._sentinel)}"])

    async def resume_probe(self) -> bool:
        """Cheap startup turn that re-attaches to the previous conversation.

        This is the literal "run ``claude --resume`` on startup" behaviour.  It is a real
        (if tiny) Claude turn, so it is opt-in via ``resume_probe`` in config.json.
        """
        return await self.send_prompt(
            "Reply with exactly: READY. Do not use any tools or explain anything."
        )

    # -- reading ----------------------------------------------------------------

    async def _pump(self, stream: asyncio.StreamReader | None, kind: Kind) -> None:
        """Continuously read *stream* in chunks and dispatch complete lines.

        Chunked reads (rather than ``readline``) matter here: a partial line with no
        trailing newline -- a progress spinner, a prompt fragment -- would otherwise sit
        invisible in the buffer instead of reaching the pane.
        """
        if stream is None:
            return
        buffer = ""
        try:
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                # Normalise CRLF/CR so Windows output does not produce blank lines.
                buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    self._handle_line(line, kind)
            if buffer.strip():  # trailing fragment at EOF
                self._handle_line(buffer, kind)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._emit("error", f"reader stopped: {exc}")

    def _handle_line(self, line: str, kind: Kind) -> None:
        """Classify and forward a single line of shell output."""
        # 1. Completion sentinel -> the turn is over.
        if self._sentinel in line:
            line = line.replace(self._sentinel, "").strip()
            self._on_turn_finished()
            if not line:
                return

        stripped = line.strip()
        if not stripped:
            return

        # 2. PowerShell echoes its own prompt when driven from stdin -- drop it.
        if _is_shell_prompt(stripped):
            return

        # 3. Structured stream-json events.
        if self.config.stream_json and stripped.startswith("{") and stripped.endswith("}"):
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(event, dict):
                    for event_kind, text in self._render_event(event):
                        self._emit(event_kind, text)
                    return

        # 4. Anything else: raw shell output.
        self._emit(kind, stripped)
        if kind == "stderr":
            self._note_possible_resume_failure(stripped)

    def _on_turn_finished(self) -> None:
        """Sentinel seen: drop back to Idle (unless a retry is queued)."""
        if self._status is SessionStatus.WORKING:
            self._set_status(SessionStatus.IDLE)

    def _note_possible_resume_failure(self, text: str) -> None:
        """Retry once without ``--continue`` when there is no conversation to resume."""
        lowered = text.lower()
        no_session = (
            "no conversation found" in lowered
            or "no previous conversation" in lowered
            or "no sessions found" in lowered
            or ("session" in lowered and "not found" in lowered)
        )
        if not no_session:
            return
        if self._retried_without_continue or not self._last_prompt:
            return
        self._retried_without_continue = True
        self._continue_next = False
        self.session_id = None
        prompt = self._last_prompt
        self._emit("info", "nothing to resume here -- starting a fresh Claude session")

        async def _retry() -> None:
            await asyncio.sleep(0.2)  # let the sentinel land first
            await self._dispatch(prompt)

        asyncio.get_running_loop().create_task(_retry())

    # -- stream-json rendering --------------------------------------------------

    def _render_event(self, event: dict) -> Iterator[tuple[Kind, str]]:
        """Translate one stream-json event into display lines."""
        etype = event.get("type")

        if etype == "system":
            if event.get("subtype") == "init":
                self.session_id = event.get("session_id") or self.session_id
                model = event.get("model") or "?"
                tools = len(event.get("tools") or [])
                short = (self.session_id or "?")[:8]
                yield ("info", f"session {short} - model {model} - {tools} tools")
            else:
                subtype = event.get("subtype") or "system"
                yield ("info", f"[{subtype}]")
            return

        if etype == "assistant":
            for block in _content_blocks(event):
                btype = block.get("type")
                if btype == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        yield ("assistant", text)
                elif btype == "thinking":
                    yield ("thinking", "(thinking)")
                elif btype == "tool_use":
                    name = block.get("name") or "tool"
                    summary = _summarise_tool_input(name, block.get("input") or {})
                    yield ("tool", f"* {name}" + (f"  {summary}" if summary else ""))
            return

        if etype == "user":
            # Tool results come back as synthetic user messages.
            for block in _content_blocks(event):
                if block.get("type") != "tool_result":
                    continue
                payload = block.get("content")
                text = _stringify_tool_result(payload)
                failed = bool(block.get("is_error"))
                head = " ".join(text.split())
                if len(head) > 140:
                    head = head[:137] + "..."
                prefix = "  !" if failed else "  <"
                yield ("error" if failed else "tool_result", f"{prefix} {head or '(empty)'}")
            return

        if etype == "result":
            self.session_id = event.get("session_id") or self.session_id
            self.turns += 1
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                self.last_cost_usd = float(cost)
            duration = event.get("duration_ms")
            bits: list[str] = []
            if isinstance(duration, (int, float)):
                bits.append(f"{duration / 1000:.1f}s")
            if isinstance(cost, (int, float)):
                bits.append(f"${cost:.4f}")
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                tokens = usage.get("output_tokens")
                if isinstance(tokens, int):
                    bits.append(f"{tokens} out")
            suffix = f" ({', '.join(bits)})" if bits else ""

            if event.get("is_error") or event.get("subtype") not in (None, "success"):
                reason = event.get("subtype") or "error"
                detail = _stringify_tool_result(event.get("result")) or reason
                yield ("error", f"turn failed: {' '.join(detail.split())[:200]}{suffix}")
                self._note_possible_resume_failure(detail)
            else:
                # In plain-text mode the final answer only arrives with this event.
                if not self.config.stream_json:
                    final = _stringify_tool_result(event.get("result")).strip()
                    if final:
                        yield ("assistant", final)
                yield ("result", f"done{suffix}")
            return

        if etype == "stream_event":  # --include-partial-messages deltas
            delta = (event.get("event") or {}).get("delta") or {}
            piece = delta.get("text")
            if isinstance(piece, str) and piece.strip():
                yield ("assistant", piece.rstrip())
            return

        # Unknown event type: show something rather than silently dropping it.
        yield ("stdout", f"[{etype or 'event'}]")


def _content_blocks(event: dict) -> list[dict]:
    """Extract the content-block list from an assistant/user stream-json event."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else event.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _stringify_tool_result(payload: object) -> str:
    """Best-effort flattening of a tool_result / result payload into text."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts: list[str] = []
        for item in payload:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return " ".join(part for part in parts if part)
    if isinstance(payload, dict):
        return str(payload.get("text") or payload.get("message") or payload)
    return str(payload)


def _is_shell_prompt(line: str) -> bool:
    """True for PowerShell's own echoed prompt (``PS D:\\path>``)."""
    if not line.startswith("PS "):
        return False
    return line.rstrip().endswith(">") or "> " in line[:80]


# --------------------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------------------


class SessionManager:
    """Owns one :class:`ClaudeSession` per project and tears them all down on exit."""

    def __init__(
        self,
        config: AppConfig,
        on_output: OutputCallback,
        on_status: StatusCallback,
    ) -> None:
        self.config = config
        self._on_output = on_output
        self._on_status = on_status
        self._sessions: dict[str, ClaudeSession] = {}

    def __iter__(self):
        return iter(self._sessions.values())

    def get(self, project_id: str) -> ClaudeSession | None:
        return self._sessions.get(project_id)

    def ensure(self, project: Project) -> ClaudeSession:
        """Return the session for *project*, creating the object if needed."""
        session = self._sessions.get(project.id)
        if session is None:
            session = ClaudeSession(project, self.config, self._on_output, self._on_status)
            self._sessions[project.id] = session
        else:
            session.project = project  # pick up edits from the Project Manager tab
        return session

    async def start(self, project: Project) -> bool:
        return await self.ensure(project).start()

    async def stop(self, project_id: str) -> None:
        session = self._sessions.get(project_id)
        if session is not None:
            await session.stop()

    async def discard(self, project_id: str) -> None:
        """Stop a session and forget it (used when a project is deleted)."""
        session = self._sessions.pop(project_id, None)
        if session is not None:
            await session.stop()

    async def start_all(self, projects: list[Project]) -> list[bool]:
        """Spawn every given project's shell concurrently."""
        if not projects:
            return []
        return list(
            await asyncio.gather(
                *(self.start(project) for project in projects), return_exceptions=False
            )
        )

    async def stop_all(self) -> None:
        """Clean teardown of every background process -- no orphans left behind."""
        sessions = list(self._sessions.values())
        await asyncio.gather(*(session.stop() for session in sessions), return_exceptions=True)
        # Belt and braces: anything still tracked gets taskkill'd.
        for pid in list(_LIVE_PIDS):
            kill_process_tree(pid)

    def busy_count(self) -> int:
        return sum(1 for session in self._sessions.values() if session.is_busy)

    def running_count(self) -> int:
        return sum(1 for session in self._sessions.values() if session.is_running)
