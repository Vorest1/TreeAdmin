import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(
        self,
        node_id,
        session_ttl_seconds=15 * 60,
        session_cleaner_interval=30,
    ):
        # type: (str, int, int) -> None
        if not sys.platform.startswith("linux"):
            raise RuntimeError("TreeAdmin server shell sessions support Linux only")

        self.node_id = node_id
        self.session_ttl_seconds = session_ttl_seconds
        self.session_cleaner_interval = session_cleaner_interval

        self.command_timeout_seconds = self._env_float("TREEADMIN_COMMAND_TIMEOUT", 0.0)
        self.command_output_limit_bytes = self._env_int(
            "TREEADMIN_COMMAND_OUTPUT_LIMIT",
            512 * 1024,
        )

        self._sessions = {}  # type: Dict[str, Dict[str, Any]]
        self._next_session_id = 1
        self._lock = threading.RLock()

        # log
        logger.info(
            "session manager initialized : node_id=%s session_ttl_seconds=%s "
            "session_cleaner_interval=%s command_timeout_seconds=%s "
            "command_output_limit_bytes=%s platform=linux",
            self.node_id,
            self.session_ttl_seconds,
            self.session_cleaner_interval,
            self.command_timeout_seconds,
            self.command_output_limit_bytes,
        )
        #

    @staticmethod
    def _env_int(name, default):
        # type: (str, int) -> int
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default

        try:
            return max(0, int(raw))
        except ValueError:
            # log
            logger.warning(
                "invalid integer env value ignored : name=%s value=%s default=%s",
                name,
                raw,
                default,
            )
            #
            return default

    @staticmethod
    def _env_float(name, default):
        # type: (str, float) -> float
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default

        try:
            return max(0.0, float(raw))
        except ValueError:
            # log
            logger.warning(
                "invalid float env value ignored : name=%s value=%s default=%s",
                name,
                raw,
                default,
            )
            #
            return default

    @staticmethod
    def close_session_resources_static(session):
        # type: (Dict[str, Any]) -> None
        process = session.get("process")

        if process is None:
            return

        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
        except Exception:
            # log
            logger.warning(
                "linux session process graceful close failed",
                exc_info=True,
            )
            #

            try:
                os.killpg(process.pid, signal.SIGKILL)

                # log
                logger.warning("linux session process killed")
                #
            except Exception:
                # log
                logger.exception("linux session process kill failed")
                #

    def open_session(self):
        # type: () -> Dict[str, Any]
        with self._lock:
            session_id = str(self._next_session_id)
            self._next_session_id += 1

        now = time.time()
        start_cwd = str(Path.home())

        session = {
            "platform": "linux",
            "process": None,
            "lock": threading.Lock(),
            "cwd": start_cwd,
            "previous_cwd": start_cwd,
            "created_at": now,
            "last_activity": now,
            "node_id": self.node_id,
        }

        with self._lock:
            self._sessions[session_id] = session
            sessions_count = len(self._sessions)

        # log
        logger.info(
            "session opened : session_id=%s platform=linux cwd=%s sessions_count=%s",
            session_id,
            session.get("cwd"),
            sessions_count,
        )
        #

        return {
            "session_id": session_id,
            "cwd": session["cwd"],
            "node_id": self.node_id,
        }

    def get_session(self, session_id):
        # type: (str) -> Optional[Dict[str, Any]]
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions(self):
        # type: () -> List[Dict[str, Any]]
        with self._lock:
            items = list(self._sessions.items())

        result = []  # type: List[Dict[str, Any]]

        for session_id, session in items:
            result.append(
                {
                    "session_id": session_id,
                    "cwd": str(session.get("cwd", "")),
                    "platform": str(session.get("platform", "linux")),
                    "created_at": session.get("created_at"),
                    "last_activity": session.get("last_activity"),
                    "node_id": str(session.get("node_id", self.node_id)),
                }
            )

        return result

    def touch_session(self, session_id):
        # type: (str) -> bool
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False

            session["last_activity"] = time.time()
            return True

    def close_session(self, session_id):
        # type: (str) -> Optional[Dict[str, Any]]
        with self._lock:
            session = self._sessions.pop(session_id, None)
            sessions_count = len(self._sessions)

        if session is None:
            # log
            logger.warning(
                "session close skipped [not found] : session_id=%s",
                session_id,
            )
            #
            return None

        try:
            self.close_session_resources_static(session)
        except Exception:
            # log
            logger.exception(
                "session close resources failed : session_id=%s",
                session_id,
            )
            #
            raise

        # log
        logger.info(
            "session closed : session_id=%s platform=%s cwd=%s sessions_count=%s",
            session_id,
            session.get("platform"),
            session.get("cwd"),
            sessions_count,
        )
        #

        return session

    @staticmethod
    def _strip_quotes(value):
        # type: (str) -> str
        value = value.strip()

        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            return value[1:-1]

        return value

    def _resolve_linux_cd_target(
        self,
        session,
        cwd,
        target,
    ):
        # type: (Dict[str, Any], str, str) -> Tuple[str, Optional[str]]
        target = self._strip_quotes(target)
        home = str(Path.home())

        if not target or target == "~":
            return home, None

        if target == "-":
            previous = str(session.get("previous_cwd", "") or home)
            return previous, previous

        expanded_target = os.path.expanduser(target)

        if os.path.isabs(expanded_target):
            candidate = os.path.abspath(expanded_target)
        else:
            candidate = os.path.abspath(os.path.join(cwd, expanded_target))

        return candidate, None

    def _append_limited_output(
        self,
        output_parts,
        chunk,
        captured_size,
        limit,
    ):
        # type: (List[bytes], bytes, int, int) -> Tuple[int, bool]
        if not chunk:
            return captured_size, False

        if limit <= 0:
            output_parts.append(chunk)
            return captured_size + len(chunk), False

        if captured_size >= limit:
            return captured_size, True

        remaining = limit - captured_size
        output_parts.append(chunk[:remaining])
        captured_size += min(len(chunk), remaining)

        if len(chunk) > remaining:
            return captured_size, True

        return captured_size, False

    def _terminate_linux_process_group(
        self,
        process,
        command,
        cwd,
        timeout,
    ):
        # type: (subprocess.Popen, str, str, float) -> None
        # log
        logger.warning(
            "linux command timeout : cwd=%s timeout=%s command=[%s]",
            cwd,
            timeout,
            command,
        )
        #

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except Exception:
            # log
            logger.debug(
                "linux command terminate process group failed : pid=%s command=[%s]",
                process.pid,
                command,
                exc_info=True,
            )
            #

        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            # log
            logger.debug(
                "linux command kill process group failed : pid=%s command=[%s]",
                process.pid,
                command,
                exc_info=True,
            )
            #

        try:
            process.wait(timeout=3)
        except Exception:
            # log
            logger.debug(
                "linux command wait after kill failed : pid=%s command=[%s]",
                process.pid,
                command,
                exc_info=True,
            )
            #

    def _run_linux_command_limited(self, command, cwd):
        # type: (str, str) -> Tuple[str, Optional[int]]
        try:
            process = subprocess.Popen(
                ["/bin/bash", "-lc", command],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            # log
            logger.exception(
                "linux command start failed : cwd=%s command=[%s]",
                cwd,
                command,
            )
            #
            raise

        if process.stdout is None:
            # log
            logger.error(
                "linux command start failed [missing stdout pipe] : cwd=%s command=[%s]",
                cwd,
                command,
            )
            #
            raise RuntimeError("process stdout pipe is missing")

        output_parts = []  # type: List[bytes]
        captured_size = 0
        observed_size = 0
        truncated = False
        timed_out = False

        timeout = self.command_timeout_seconds
        deadline = time.monotonic() + timeout if timeout > 0 else None
        limit = self.command_output_limit_bytes
        fd = process.stdout.fileno()

        # log
        logger.debug(
            "linux command started : pid=%s cwd=%s timeout=%s output_limit=%s command=[%s]",
            process.pid,
            cwd,
            timeout,
            limit,
            command,
        )
        #

        while True:
            if (
                deadline is not None
                and not timed_out
                and time.monotonic() > deadline
                and process.poll() is None
            ):
                timed_out = True
                self._terminate_linux_process_group(process, command, cwd, timeout)

            try:
                readable, _, _ = select.select([fd], [], [], 0.05)
            except (OSError, ValueError):
                readable = []

            if readable:
                try:
                    chunk = os.read(fd, 8192)
                except BlockingIOError:
                    chunk = b""
                except OSError:
                    chunk = b""

                if chunk:
                    observed_size += len(chunk)
                    captured_size, was_truncated = self._append_limited_output(
                        output_parts,
                        chunk,
                        captured_size,
                        limit,
                    )
                    truncated = truncated or was_truncated
                    continue

            if process.poll() is not None:
                while True:
                    try:
                        chunk = os.read(fd, 8192)
                    except BlockingIOError:
                        chunk = b""
                    except OSError:
                        chunk = b""

                    if not chunk:
                        break

                    observed_size += len(chunk)
                    captured_size, was_truncated = self._append_limited_output(
                        output_parts,
                        chunk,
                        captured_size,
                        limit,
                    )
                    truncated = truncated or was_truncated

                break

        returncode = process.returncode
        output_raw = b"".join(output_parts)
        output = output_raw.decode("utf-8", errors="replace")

        if truncated:
            output += (
                "\n\n[TreeAdmin: command output truncated in executor; "
                "captured_limit={} bytes; observed_output_size={} bytes]".format(
                    limit,
                    observed_size,
                )
            )

        if timed_out:
            output += "\n\n[TreeAdmin: command timed out after {} seconds]".format(timeout)
            if returncode is None:
                returncode = 124

        # log
        logger.debug(
            "linux command finished : pid=%s cwd=%s returncode=%s output_size=%s "
            "captured_size=%s truncated=%s timed_out=%s command=[%s]",
            process.pid,
            cwd,
            returncode,
            observed_size,
            captured_size,
            truncated,
            timed_out,
            command,
        )
        #

        return output.strip(), returncode

    def execute(self, session_id, command):
        # type: (str, str) -> Tuple[str, str, Optional[int]]
        session = self.get_session(session_id)
        if not session:
            # log
            logger.warning(
                "session execute skipped [not_found] : session_id=%s node_id=%s",
                session_id,
                self.node_id,
            )
            #
            return "session not found", "", None

        with session["lock"]:
            session["last_activity"] = time.time()

            cwd = str(session.get("cwd", str(Path.home()))).strip() or str(Path.home())
            cmd = command.strip()

            try:
                if not cmd:
                    output, new_cwd, returncode = "", cwd, 0

                elif cmd == "cd" or cmd.startswith("cd "):
                    raw_target = "" if cmd == "cd" else cmd[3:].strip()
                    candidate, echo_value = self._resolve_linux_cd_target(
                        session,
                        cwd,
                        raw_target,
                    )

                    if not os.path.isdir(candidate):
                        output, new_cwd, returncode = (
                            "cd: no such directory: {}".format(raw_target),
                            cwd,
                            1,
                        )
                    else:
                        session["previous_cwd"] = cwd
                        output = echo_value or ""
                        new_cwd = candidate
                        returncode = 0

                else:
                    output, returncode = self._run_linux_command_limited(command, cwd)
                    new_cwd = cwd

                session["cwd"] = new_cwd
                session["last_activity"] = time.time()

                return output.strip(), new_cwd, returncode

            except Exception:
                # log
                logger.exception(
                    "session execute failed : session_id=%s node_id=%s "
                    "platform=linux command=[%s] cwd=%s",
                    session_id,
                    self.node_id,
                    command,
                    cwd,
                )
                #
                raise

    def pop_expired_sessions(self):
        # type: () -> List[Tuple[str, Dict[str, Any]]]
        now = time.time()
        expired = []  # type: List[Tuple[str, Dict[str, Any]]]

        with self._lock:
            for session_id, session in list(self._sessions.items()):
                last_activity = float(session.get("last_activity", now))
                if now - last_activity > self.session_ttl_seconds:
                    expired.append((session_id, self._sessions.pop(session_id)))

        if expired:
            # log
            logger.info(
                "expired sessions popped : node_id=%s count=%s",
                self.node_id,
                len(expired),
            )
            #

        return expired