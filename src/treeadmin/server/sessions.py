from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
import uuid
from pathlib import Path


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
    def _normalize_callback(callback: dict | None) -> dict | None:
        if not isinstance(callback, dict):
            return None

        host = callback.get("host") or callback.get("callback_host")
        port = callback.get("port") or callback.get("callback_port")
        path = callback.get("path") or callback.get("callback_path") or "/deliver_result"

        if not isinstance(host, str) or not host.strip():
            return None

        try:
            port = int(port)
        except Exception:
            return None

        if port <= 0:
            return None

        if not isinstance(path, str) or not path.startswith("/"):
            path = "/deliver_result"

        return {
            "host": host.strip(),
            "port": port,
            "path": path,
        }

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
                try:
                    process.kill()
                except Exception:
                    pass

    def open_session(self, callback: dict | None = None) -> dict:
        with self._lock:
            session_id = str(self._next_session_id)
            self._next_session_id += 1

        platform_name = self._get_platform_name()
        now = time.time()
        callback_info = self._normalize_callback(callback)

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
                "client_callback": callback_info,
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
                "client_callback": callback_info,
            }

        with self._lock:
            self._sessions[session_id] = session

        return {
            "session_id": session_id,
            "cwd": session["cwd"],
            "node_id": self.node_id,
            "client_callback": callback_info,
        }

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
                    "client_callback": session.get("client_callback"),
                }
            )
        return result

    def update_callback(self, session_id: str, callback: dict | None) -> dict | None:
        callback_info = self._normalize_callback(callback)
        if callback_info is None:
            return None

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session["client_callback"] = callback_info
            session["last_activity"] = time.time()

        return callback_info

    def close_session(self, session_id: str) -> dict | None:
        with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is not None:
            self.close_session_resources_static(session)

        return session

    def execute(self, session_id: str, command: str) -> tuple[str, str, int | None]:
        session = self.get_session(session_id)
        if not session:
            return "session not found", "", None

        with session["lock"]:
            session["last_activity"] = time.time()

            if session.get("platform") == "windows":
                process = session.get("process")

                if process is None:
                    raise RuntimeError("windows shell process is missing")
                if process.poll() is not None:
                    raise RuntimeError("shell process already terminated")

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

            cwd = str(session.get("cwd", str(Path.home()))).strip() or str(Path.home())
            cmd = command.strip()

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

    def pop_expired_sessions(self) -> list[tuple[str, dict]]:
        now = time.time()
        expired: list[tuple[str, dict]] = []

        with self._lock:
            for session_id, session in list(self._sessions.items()):
                last_activity = float(session.get("last_activity", now))
                if now - last_activity > self.session_ttl_seconds:
                    expired.append((session_id, self._sessions.pop(session_id)))

        return expired