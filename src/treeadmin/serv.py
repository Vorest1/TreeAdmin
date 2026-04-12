from __future__ import annotations

import http.client
import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.treeadmin.routing import (
    build_forward_request,
    format_route,
    get_current_hop,
    is_final_hop,
    parse_route_params,
)


SESSION_TTL_SECONDS = 15 * 60
SESSION_CLEANER_INTERVAL = 30
EVENT_TTL_SECONDS = 48 * 60 * 60
EVENT_HEARTBEAT_SECONDS = 15
DATA_DIR = Path("data")
JOBS_FILE = DATA_DIR / "server_jobs.json"


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _ensure_state_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not JOBS_FILE.exists():
        _atomic_write_json(
            JOBS_FILE,
            {
                "next_job_id": 1,
                "next_event_id": 1,
                "queue": [],
                "history": [],
                "responses": [],
            },
        )


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def _load_state() -> dict:
    _ensure_state_file()
    raw = JOBS_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return {
            "next_job_id": 1,
            "next_event_id": 1,
            "queue": [],
            "history": [],
            "responses": [],
        }
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("server_jobs.json root must be object")
    data.setdefault("next_job_id", 1)
    data.setdefault("next_event_id", 1)
    data.setdefault("queue", [])
    data.setdefault("history", [])
    data.setdefault("responses", [])
    return data


def _save_state(state: dict) -> None:
    _atomic_write_json(JOBS_FILE, state)


def _purge_expired_responses_locked(state: dict) -> bool:
    now = time.time()
    original_len = len(state["responses"])
    state["responses"] = [
        item
        for item in state["responses"]
        if float(item.get("expires_at_ts", now + EVENT_TTL_SECONDS)) > now
    ]
    return len(state["responses"]) != original_len


def _append_response_locked(httpd, state: dict, job: dict) -> dict:
    event_id = str(state["next_event_id"])
    state["next_event_id"] += 1

    response = {
        "event_id": event_id,
        "job_id": job["job_id"],
        "session_id": job["session_id"],
        "node_id": job.get("node_id", ""),
        "command": job["command"],
        "status": job["status"],
        "output": job.get("output", ""),
        "cwd": job.get("cwd", ""),
        "returncode": job.get("returncode"),
        "error": job.get("error"),
        "ready_at": job.get("finished_at", _utc_now()),
        "created_at": _utc_now(),
        "expires_at_ts": time.time() + EVENT_TTL_SECONDS,
        "delivered": False,
        "delivered_at": None,
        "acked": False,
        "acked_at": None,
    }
    state["responses"].append(response)

    try:
        with httpd.events_condition:
            httpd.events_condition.notify_all()
    except Exception:
        pass

    return response


def _mark_startup_orphans(httpd) -> None:
    with httpd.jobs_lock:
        state = _load_state()
        if not state["queue"]:
            _purge_expired_responses_locked(state)
            _save_state(state)
            return

        orphaned: list[dict] = []
        while state["queue"]:
            job = state["queue"].pop(0)
            job.update(
                {
                    "status": "orphaned",
                    "started_at": job.get("started_at") or _utc_now(),
                    "finished_at": _utc_now(),
                    "output": "",
                    "error": "server restarted before queued command could finish",
                    "returncode": None,
                    "cwd": "",
                }
            )
            state["history"].append(job)
            orphaned.append(job)

        for job in orphaned:
            _append_response_locked(httpd, state, job)

        _purge_expired_responses_locked(state)
        _save_state(state)


def _execute_in_session_server(httpd, session_id: str, command: str) -> tuple[str, str, int | None]:
    session = httpd.shell_sessions.get(session_id)
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

            cwd = cwd_raw or session.get("cwd", "")
            session["cwd"] = cwd
            session["last_activity"] = time.time()
            return output, cwd, returncode

        cwd = session.get("cwd", str(Path.home()))
        if not isinstance(cwd, str) or not cwd.strip():
            cwd = str(Path.home())

        cmd = command.strip()
        if not cmd:
            output, cwd, returncode = "", cwd, 0
        elif cmd == "cd":
            output, cwd, returncode = "", str(Path.home()), 0
        elif cmd.startswith("cd "):
            target = cmd[3:].strip()
            if (target.startswith('"') and target.endswith('"')) or (
                target.startswith("'") and target.endswith("'")
            ):
                target = target[1:-1]

            if not target:
                output, cwd, returncode = "", str(Path.home()), 0
            else:
                if os.path.isabs(target):
                    candidate = os.path.abspath(target)
                else:
                    candidate = os.path.abspath(os.path.join(cwd, target))

                if not os.path.isdir(candidate):
                    output, returncode = f"cd: no such directory: {target}", 1
                else:
                    output, cwd, returncode = "", candidate, 0
        else:
            completed = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
            )
            output = completed.stdout or ""
            returncode = completed.returncode

        session["cwd"] = cwd
        session["last_activity"] = time.time()
        return output.strip(), cwd, returncode


def _claim_next_job_for_session_locked(state: dict, session_id: str) -> dict | None:
    for job in state["queue"]:
        if job.get("session_id") != session_id:
            continue
        if job.get("status") != "queued":
            continue
        job["status"] = "running"
        job["started_at"] = _utc_now()
        return job
    return None


def _finalize_job_locked(httpd, state: dict, job: dict) -> None:
    queue = state["queue"]
    state["queue"] = [item for item in queue if item.get("job_id") != job.get("job_id")]
    state["history"].append(job)
    _append_response_locked(httpd, state, job)


def _cancel_queued_jobs_for_session(httpd, session_id: str, reason: str) -> None:
    with httpd.jobs_lock:
        state = _load_state()
        changed = False
        remaining_queue: list[dict] = []
        for job in state["queue"]:
            if job.get("session_id") != session_id or job.get("status") == "running":
                remaining_queue.append(job)
                continue

            job.update(
                {
                    "status": "canceled",
                    "finished_at": _utc_now(),
                    "output": "",
                    "cwd": "",
                    "returncode": None,
                    "error": reason,
                }
            )
            state["history"].append(job)
            _append_response_locked(httpd, state, job)
            changed = True

        if changed:
            state["queue"] = remaining_queue
            _purge_expired_responses_locked(state)
            _save_state(state)


def _session_worker_loop(httpd, session_id: str) -> None:
    while True:
        session = httpd.shell_sessions.get(session_id)
        if session is None:
            return

        claimed_job: dict | None = None
        with httpd.jobs_lock:
            state = _load_state()
            claimed_job = _claim_next_job_for_session_locked(state, session_id)
            _purge_expired_responses_locked(state)
            _save_state(state)

        if claimed_job is None:
            time.sleep(0.3)
            continue

        try:
            output, cwd, returncode = _execute_in_session_server(
                httpd,
                session_id,
                claimed_job["command"],
            )
            claimed_job.update(
                {
                    "status": "finished" if not isinstance(returncode, int) or returncode == 0 else "failed",
                    "finished_at": _utc_now(),
                    "output": output,
                    "cwd": cwd,
                    "returncode": returncode,
                    "error": None,
                }
            )
        except Exception as e:
            claimed_job.update(
                {
                    "status": "failed",
                    "finished_at": _utc_now(),
                    "output": "",
                    "cwd": "",
                    "returncode": None,
                    "error": str(e),
                }
            )

        with httpd.jobs_lock:
            state = _load_state()
            _finalize_job_locked(httpd, state, claimed_job)
            _purge_expired_responses_locked(state)
            _save_state(state)


def _start_session_worker(httpd, session_id: str) -> None:
    worker = threading.Thread(
        target=_session_worker_loop,
        args=(httpd, session_id),
        daemon=True,
        name=f"session-worker-{session_id}",
    )
    worker.start()
    httpd.session_workers[session_id] = worker


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            self.close_connection = True

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_text(self, status: int, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _forward(self, host: str, port: int, method: str, path: str, body: bytes) -> None:
        conn = http.client.HTTPConnection(host, port, timeout=60)
        try:
            headers = {}
            content_type = self.headers.get("Content-Type")
            if content_type:
                headers["Content-Type"] = content_type

            last_event_id = self.headers.get("Last-Event-ID")
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id

            headers["Content-Length"] = str(len(body)) if body else "0"

            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_content_type = resp.getheader("Content-Type", "text/plain; charset=utf-8")

            if "text/event-stream" in resp_content_type:
                self.send_response(resp.status)
                self.send_header("Content-Type", resp_content_type)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.close_connection = False

                try:
                    while True:
                        chunk = resp.read(1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    self.close_connection = True
                    return
                finally:
                    self.close_connection = True
                return

            resp_body = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp_content_type)
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(resp_body)
            self.close_connection = True

        except Exception as e:
            self._send_text(502, f"proxy error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _extract_extra_query(self, qs: dict[str, list[str]]) -> dict[str, str]:
        extra: dict[str, str] = {}
        for key, values in qs.items():
            if key in {"route", "hop"}:
                continue
            extra[key] = values[0] if values else ""
        return extra

    def _resolve_route(self, parsed):
        qs = parse_qs(parsed.query, keep_blank_values=True)
        hops, hop_index = parse_route_params(qs)

        if not hops:
            raise ValueError("empty route")

        get_current_hop(hops, hop_index)
        return qs, hops, hop_index

    def _load_json_payload(self, body: bytes) -> dict:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ValueError(f"expected application/json: {content_type}")

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"bad json: {e}") from e

        if not isinstance(payload, dict):
            raise ValueError("json body must be object")

        return payload

    def _get_platform_name(self) -> str:
        system_name = platform.system().lower()
        if system_name.startswith("win"):
            return "windows"
        if system_name in {"linux", "darwin"}:
            return "posix"
        return "posix"

    def _close_session_resources(self, session: dict) -> None:
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

    def _handle_open_shell(self) -> None:
        session_id = str(self.server.next_session_id)
        self.server.next_session_id += 1

        platform_name = self._get_platform_name()
        now = time.time()

        if platform_name == "windows":
            start_cwd = "C:/Users/user/Desktop"
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

            self.server.shell_sessions[session_id] = {
                "platform": "windows",
                "process": process,
                "lock": threading.Lock(),
                "cwd": cwd_raw or start_cwd,
                "created_at": now,
                "last_activity": now,
                "node_id": self.server.node_id,
            }
        else:
            start_cwd = str(Path.home())
            self.server.shell_sessions[session_id] = {
                "platform": "posix",
                "process": None,
                "lock": threading.Lock(),
                "cwd": start_cwd,
                "created_at": now,
                "last_activity": now,
                "node_id": self.server.node_id,
            }

        _start_session_worker(self.server, session_id)
        session = self.server.shell_sessions[session_id]
        self._send_json(
            200,
            {
                "session_id": session_id,
                "cwd": session["cwd"],
                "node_id": self.server.node_id,
            },
        )

    def _handle_send_command(self, payload: dict) -> None:
        session_id = str(payload.get("session_id", "")).strip()
        command = str(payload.get("command", "")).strip()

        if not session_id:
            self._send_text(400, "missing 'session_id'")
            return
        if not command:
            self._send_text(400, "missing 'command'")
            return

        session = self.server.shell_sessions.get(session_id)
        if not session:
            self._send_text(404, "session not found")
            return

        with self.server.jobs_lock:
            state = _load_state()
            job_id = str(state["next_job_id"])
            state["next_job_id"] += 1
            job = {
                "job_id": job_id,
                "session_id": session_id,
                "node_id": self.server.node_id,
                "command": command,
                "status": "queued",
                "created_at": _utc_now(),
                "started_at": None,
                "finished_at": None,
                "output": "",
                "cwd": session.get("cwd", ""),
                "returncode": None,
                "error": None,
            }
            state["queue"].append(job)
            _purge_expired_responses_locked(state)
            _save_state(state)

        self._send_json(
            202,
            {
                "job_id": job_id,
                "session_id": session_id,
                "status": "queued",
                "cwd": session.get("cwd", ""),
                "node_id": self.server.node_id,
            },
        )

    def _handle_close_shell(self, payload: dict) -> None:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            self._send_text(400, "missing 'session_id'")
            return

        session = self.server.shell_sessions.pop(session_id, None)
        if not session:
            self._send_text(404, "session not found")
            return

        _cancel_queued_jobs_for_session(self.server, session_id, "session closed")
        self._close_session_resources(session)
        self._send_text(200, "shell closed")

    def _handle_list_sessions(self) -> None:
        sessions = []
        for session_id, session in self.server.shell_sessions.items():
            sessions.append(
                {
                    "session_id": session_id,
                    "cwd": session.get("cwd", ""),
                    "platform": session.get("platform", ""),
                    "created_at": session.get("created_at"),
                    "last_activity": session.get("last_activity"),
                    "node_id": session.get("node_id", self.server.node_id),
                }
            )
        self._send_json(200, {"sessions": sessions})

    def _handle_session_jobs(self, session_id: str) -> None:
        with self.server.jobs_lock:
            state = _load_state()
            _purge_expired_responses_locked(state)
            _save_state(state)
            queue_jobs = [job for job in state["queue"] if job.get("session_id") == session_id]
            history_jobs = [job for job in state["history"] if job.get("session_id") == session_id]
            responses = [
                item
                for item in state["responses"]
                if item.get("session_id") == session_id and not item.get("acked", False)
            ]

        self._send_json(
            200,
            {
                "session_id": session_id,
                "queue": queue_jobs,
                "history": history_jobs,
                "responses": responses,
            },
        )

    def _handle_ack_event(self, payload: dict) -> None:
        event_id = str(payload.get("event_id", "")).strip()
        if not event_id:
            self._send_text(400, "missing 'event_id'")
            return

        removed = False
        with self.server.jobs_lock:
            state = _load_state()
            new_responses = []
            for item in state["responses"]:
                if item.get("event_id") == event_id:
                    removed = True
                    continue
                new_responses.append(item)
            state["responses"] = new_responses
            _purge_expired_responses_locked(state)
            _save_state(state)

        if not removed:
            self._send_text(404, "event not found")
            return

        self._send_text(200, "acknowledged")

    def _iter_pending_events(self, session_id: str | None, last_event_id: str | None) -> list[dict]:
        last_id_num = -1
        if isinstance(last_event_id, str) and last_event_id.strip():
            try:
                last_id_num = int(last_event_id.strip())
            except ValueError:
                last_id_num = -1

        with self.server.jobs_lock:
            state = _load_state()
            changed = _purge_expired_responses_locked(state)
            responses = [item for item in state["responses"] if not item.get("acked", False)]
            if session_id:
                responses = [item for item in responses if item.get("session_id") == session_id]
            if last_id_num >= 0:
                responses = [
                    item
                    for item in responses
                    if int(str(item.get("event_id", "0"))) > last_id_num
                ]
            responses = sorted(responses, key=lambda item: int(str(item.get("event_id", "0"))))
            if changed:
                _save_state(state)
        return responses

    def _handle_events(self, session_id: str | None, last_event_id: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = False

        sent_event_ids: set[str] = set()

        def send_event(event: dict) -> None:
            payload = (
                f"id: {event['event_id']}\n"
                f"event: command_result\n"
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            ).encode("utf-8")
            self.wfile.write(payload)
            self.wfile.flush()
            sent_event_ids.add(str(event["event_id"]))

        try:
            for event in self._iter_pending_events(session_id, last_event_id):
                send_event(event)

            while True:
                with self.server.events_condition:
                    self.server.events_condition.wait(timeout=EVENT_HEARTBEAT_SECONDS)

                pending = self._iter_pending_events(session_id, None)
                new_items = [item for item in pending if str(item.get("event_id")) not in sent_event_ids]

                if not new_items:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue

                for event in new_items:
                    send_event(event)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.close_connection = True

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path not in {"/hello", "/events", "/list_sessions", "/session_jobs"}:
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self._resolve_route(parsed)
        except ValueError as e:
            self._send_text(400, str(e))
            return

        print(f"ROUTE: {format_route(hops)} | hop={hop_index}")

        if not is_final_hop(hops, hop_index):
            try:
                next_host, next_port, next_path = build_forward_request(
                    endpoint_path=parsed.path,
                    hops=hops,
                    current_hop_index=hop_index,
                    extra_query=self._extract_extra_query(qs),
                )
            except ValueError as e:
                self._send_text(500, f"route forward error: {e}")
                return

            if parsed.path == "/events":
                print(f"PROXY: forward GET(stream) -> {next_host}:{next_port} {next_path}")
            else:
                print(f"PROXY: forward GET -> {next_host}:{next_port} {next_path}")
            self._forward(next_host, next_port, "GET", next_path, b"")
            return

        if parsed.path == "/hello":
            msg = (qs.get("msg") or [""])[0]
            print(f"SERVER: receive ping: {msg}")
            self._send_text(200, "Hello, Client")
            return

        if parsed.path == "/list_sessions":
            self._handle_list_sessions()
            return

        if parsed.path == "/session_jobs":
            session_id = (qs.get("session_id") or [""])[0].strip()
            if not session_id:
                self._send_text(400, "missing query parameter 'session_id'")
                return
            self._handle_session_jobs(session_id)
            return

        if parsed.path == "/events":
            session_id = (qs.get("session_id") or [""])[0].strip() or None
            last_event_id = self.headers.get("Last-Event-ID")
            self._handle_events(session_id, last_event_id)
            return

        self._send_text(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        cur_paths = {"/send_command", "/open_shell", "/close_shell", "/ack_event"}
        if parsed.path not in cur_paths:
            self._send_text(404, "not found")
            return

        try:
            qs, hops, hop_index = self._resolve_route(parsed)
        except ValueError as e:
            self._send_text(400, str(e))
            return

        body = self._read_body()
        print(f"ROUTE: {format_route(hops)} | hop={hop_index}")

        if not is_final_hop(hops, hop_index):
            try:
                next_host, next_port, next_path = build_forward_request(
                    endpoint_path=parsed.path,
                    hops=hops,
                    current_hop_index=hop_index,
                    extra_query=self._extract_extra_query(qs),
                )
            except ValueError as e:
                self._send_text(500, f"route forward error: {e}")
                return

            print(f"PROXY: forward POST -> {next_host}:{next_port} {next_path}")
            self._forward(next_host, next_port, "POST", next_path, body)
            return

        if parsed.path == "/open_shell":
            try:
                self._handle_open_shell()
            except Exception as e:
                self._send_text(500, f"open shell error: {e}")
            return

        try:
            payload = self._load_json_payload(body)
        except ValueError as e:
            self._send_text(415 if "expected application/json" in str(e) else 400, str(e))
            return

        if parsed.path == "/send_command":
            self._handle_send_command(payload)
            return
        if parsed.path == "/close_shell":
            self._handle_close_shell(payload)
            return
        if parsed.path == "/ack_event":
            self._handle_ack_event(payload)
            return

        self._send_text(404, "not found")

    def log_message(self, fmt, *args):
        sys.stdout.write(
            f'[{datetime.now().strftime("%d.%m.%Y %H:%M:%S")}] '
            f'[NODE {self.client_address[0]}] '
            f'{self.command} {urlparse(self.path).path}\n'
        )


def _cleanup_expired_sessions(httpd) -> None:
    while True:
        time.sleep(httpd.session_cleaner_interval)

        now = time.time()
        expired_ids: list[str] = []
        for session_id, session in list(httpd.shell_sessions.items()):
            last_activity = session.get("last_activity", now)
            if now - last_activity > httpd.session_ttl_seconds:
                expired_ids.append(session_id)

        for session_id in expired_ids:
            session = httpd.shell_sessions.pop(session_id, None)
            if session is None:
                continue

            try:
                _cancel_queued_jobs_for_session(httpd, session_id, "session timeout")
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

                print(f"SESSION TIMEOUT: closed inactive session {session_id}")
            except Exception as e:
                print(f"SESSION TIMEOUT ERROR: {session_id}: {e}")

        with httpd.jobs_lock:
            state = _load_state()
            if _purge_expired_responses_locked(state):
                _save_state(state)


def run_server(host: str = "0.0.0.0", port: int = 8000):
    _ensure_state_file()
    httpd = ThreadingHTTPServer((host, port), ProxyHandler)
    print(f"Server started: http://{host}:{port}")

    httpd.shell_sessions = {}
    httpd.session_workers = {}
    httpd.next_session_id = 1
    httpd.session_ttl_seconds = SESSION_TTL_SECONDS
    httpd.session_cleaner_interval = SESSION_CLEANER_INTERVAL
    httpd.jobs_lock = threading.Lock()
    httpd.events_condition = threading.Condition()
    httpd.node_id = f"{platform.node() or 'node'}:{port}"

    _mark_startup_orphans(httpd)

    cleaner = threading.Thread(
        target=_cleanup_expired_sessions,
        args=(httpd,),
        daemon=True,
        name="session-cleaner",
    )
    cleaner.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
