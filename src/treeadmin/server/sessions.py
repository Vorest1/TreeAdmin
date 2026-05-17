from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

class SessionManager:
    def __init__(
        self,
        node_id: str,
        session_ttl_seconds: int = 15 * 60,
        session_cleaner_interval: int = 30,
    ) -> None:
        self.node_id = node_id
        self.session_ttl_seconds = session_ttl_seconds
        self.session_cleaner_interval = session_cleaner_interval

        self._sessions: dict[str, dict] = {}
        self._next_session_id = 1
        self._lock = threading.RLock()

    def _get_platform_name(self) -> str:
        system_name = platform.system().lower()

        if system_name.startswith("win"):
            return "windows"

        if system_name in {"linux", "darwin"}:
            return "posix"

        return "posix"

    @staticmethod
    def close_session_resources_static(session: dict) -> None:
        platform_name = session.get("platform")
        process = session.get("process")

        if platform_name == "windows" and process is not None:
            try:
                if process.poll() is None:
                    process.stdin.write("exit\n")
                    process.stdin.flush()
                    process.wait(timeout=3)
            except Exception:
                # log
                logger.warning(
                    "session process graceful close failed",
                    platform_name,
                    exc_info=True,
                )
                #
                try:
                    process.kill()
                    # log
                    logger.warning(
                        "session process killed",
                        platform_name,
                    )
                    #
                except Exception:
                    # log
                    logger.exception("session process kill failed")
                    #
                    #pass

    def open_session(self) -> dict:
        with self._lock:
            session_id = str(self._next_session_id)
            self._next_session_id += 1

        platform_name = self._get_platform_name()
        now = time.time()

        try:
            if platform_name == "windows":
                desktop = Path.home() / "Desktop"
                start_cwd = str(desktop if desktop.exists() else Path.home())

                process = subprocess.Popen(
                    ["cmd.exe", "/Q", "/K"],
                    cwd=start_cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="cp866",
                    bufsize=1,
                )

                process.stdin.write("prompt __PROMPT__$\n")
                process.stdin.write("cd\n")
                process.stdin.write("echo __OPEN_END__\n")
                process.stdin.flush()

                lines: list[str] = []
                while True:
                    line = process.stdout.readline()
                    if not line:
                        break
                    if "__OPEN_END__" in line:
                        break
                    lines.append(line)

                cwd_raw = "".join(lines).replace("__PROMPT__", "").strip()
                if ">" in cwd_raw:
                    cwd_raw = cwd_raw.split(">")[-1].strip()

                session = {
                    "platform": "windows",
                    "process": process,
                    "lock": threading.Lock(),
                    "cwd": cwd_raw or start_cwd,
                    "created_at": now,
                    "last_activity": now,
                    "node_id": self.node_id,
                }
            else:
                start_cwd = str(Path.home())
                session = {
                    "platform": "posix",
                    "process": None,
                    "lock": threading.Lock(),
                    "cwd": start_cwd,
                    "created_at": now,
                    "last_activity": now,
                    "node_id": self.node_id,
                }

            with self._lock:
                self._sessions[session_id] = session
                sessions_count = len(self._sessions)
            
            # log
            logger.info(
                    "session opened : session_id=%s platform=%s cwd=%s sessions_count=%s",
                    session_id,
                    session.get("platform"),
                    session.get("cwd"),
                    sessions_count,
                )
            #

            return {
                "session_id": session_id,
                "cwd": session["cwd"],
                "node_id": self.node_id,
            }
        except Exception:
            # log
            logger.exception(
                "session open failed : session_id=%s platform=%s",
                session_id,
                platform_name,
            )
            #
            raise

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions(self) -> list[dict]:
        with self._lock:
            items = list(self._sessions.items())

        result: list[dict] = []
        for session_id, session in items:
            result.append(
                {
                    "session_id": session_id,
                    "cwd": str(session.get("cwd", "")),
                    "platform": str(session.get("platform", "")),
                    "created_at": session.get("created_at"),
                    "last_activity": session.get("last_activity"),
                    "node_id": str(session.get("node_id", self.node_id)),
                }
            )
        return result

    def touch_session(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session["last_activity"] = time.time()
            return True

    def close_session(self, session_id: str) -> dict | None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            sessions_count = len(self._sessions) # for log

        # log
        if session is None:
            logger.warning(
                "session close skipped [not found] : session_id=%s",
                session_id
            )
            return None

        try:
            self.close_session_resources_static(session) # not in log
        except Exception:
            logger.exception(
                "session close resources failed : session_id=%s",
                session_id
            )
            raise

        logger.info(
            "session closed : session_id=%s platform=%s cwd=%s sessions_count=%s",
            session_id,
            session.get("platform"),
            session.get("cwd"),
            sessions_count,
        )
        #

        return session

    def execute(self, session_id: str, command: str) -> tuple[str, str, int | None]:
        session = self.get_session(session_id)
        if not session:
            logger.warning(
                "session execute skipped [not_found] : session_id=%s node_id=%s",
                session_id
            )
            return "session not found", "", None

        with session["lock"]:
            session["last_activity"] = time.time()

            if session.get("platform") == "windows":
                process = session.get("process")

                if process is None:
                    # log
                    logger.error(
                        "session execute failed [missing process] : session_id=%s node_id=%s",
                        session_id,
                        self.node_id
                    )
                    #
                    raise RuntimeError("windows shell process is missing")
                if process.poll() is not None:
                    # log
                    logger.error(
                        "session execute failed [process terminated] : session_id=%s node_id=%s",
                        session_id,
                        self.node_id
                    )
                    #
                    raise RuntimeError("shell process already terminated")

                try: 
                    cmd_marker = f"__END__{uuid.uuid4().hex}__"
                    rc_marker = f"__RC__{uuid.uuid4().hex}__"
                    cwd_marker = f"__CWD__{uuid.uuid4().hex}__"

                    process.stdin.write(command + "\n")
                    process.stdin.write(f"echo {cmd_marker}\n")
                    process.stdin.write(f"echo {rc_marker}%errorlevel%\n")
                    process.stdin.flush()

                    output_lines: list[str] = []
                    while True:
                        line = process.stdout.readline()
                        if not line:
                            break
                        if cmd_marker in line:
                            break
                        output_lines.append(line)

                    returncode: int | None = None
                    while True:
                        line = process.stdout.readline()
                        if not line:
                            break
                        if rc_marker in line:
                            value = line.replace(rc_marker, "").strip()
                            try:
                                returncode = int(value)
                            except ValueError:
                                returncode = None
                            break

                    process.stdin.write("cd\n")
                    process.stdin.write(f"echo {cwd_marker}\n")
                    process.stdin.flush()

                    cwd_lines: list[str] = []
                    while True:
                        line = process.stdout.readline()
                        if not line:
                            break
                        if cwd_marker in line:
                            break
                        cwd_lines.append(line)

                    output = "".join(output_lines).replace("__PROMPT__", "").strip()
                    cwd_raw = "".join(cwd_lines).replace("__PROMPT__", "").strip()

                    if ">" in cwd_raw:
                        cwd_raw = cwd_raw.split(">")[-1].strip()

                    cwd = cwd_raw or str(session.get("cwd", ""))
                    session["cwd"] = cwd
                    session["last_activity"] = time.time()
                    return output, cwd, returncode
                except Exception:
                    logger.exception(
                        "session execute failed : session_id=%s node_id=%s platform=windows command=[%s]",
                        session_id,
                        self.node_id,
                        command
                    )
                    raise

            cwd = str(session.get("cwd", str(Path.home()))).strip() or str(Path.home())
            cmd = command.strip()

            try:
                if not cmd:
                    output, new_cwd, returncode = "", cwd, 0
                elif cmd == "cd":
                    output, new_cwd, returncode = "", str(Path.home()), 0
                elif cmd.startswith("cd "):
                    target = cmd[3:].strip()

                    if (target.startswith('"') and target.endswith('"')) or (
                        target.startswith("'") and target.endswith("'")
                    ):
                        target = target[1:-1]

                    if not target:
                        output, new_cwd, returncode = "", str(Path.home()), 0
                    else:
                        if os.path.isabs(target):
                            candidate = os.path.abspath(target)
                        else:
                            candidate = os.path.abspath(os.path.join(cwd, target))

                        if not os.path.isdir(candidate):
                            output, new_cwd, returncode = (
                                f"cd: no such directory: {target}",
                                cwd,
                                1,
                            )
                        else:
                            output, new_cwd, returncode = "", candidate, 0
                else:
                    completed = subprocess.run(
                        ["/bin/bash", "-lc", command],
                        cwd=cwd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                    )
                    output, new_cwd, returncode = (
                        completed.stdout or "",
                        cwd,
                        completed.returncode,
                    )

                session["cwd"] = new_cwd
                session["last_activity"] = time.time()
                return output.strip(), new_cwd, returncode
            except Exception:
                # log
                logger.exception(
                    "session execute failed : session_id=%s node_id=%s platform=posix command=[%s] cwd=%s",
                    session_id,
                    self.node_id,
                    command,
                    cwd
                )
                #
                raise

    def pop_expired_sessions(self) -> list[tuple[str, dict]]:
        now = time.time()
        expired: list[tuple[str, dict]] = []

        with self._lock:
            for session_id, session in list(self._sessions.items()):
                last_activity = float(session.get("last_activity", now))
                if now - last_activity > self.session_ttl_seconds:
                    expired.append((session_id, self._sessions.pop(session_id)))
        # log
        if expired:
            logger.info(
                "expired sessions popped : node_id=%s count=%s",
                self.node_id,
                len(expired),
            )
        #
        return expired