from __future__ import annotations

import time
from datetime import datetime


class JobService:
    def __init__(
        self,
        store,
        node_id: str,
        response_ttl_seconds: int = 48 * 60 * 60,
        retry_schedule_seconds: list[int] | None = None,
        notifier=None,
    ) -> None:
        self.store = store
        self.node_id = node_id
        self.response_ttl_seconds = response_ttl_seconds
        self.retry_schedule_seconds = retry_schedule_seconds or [0, 300, 3600, 18000]
        self.notifier = notifier

    @staticmethod
    def _utc_now() -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"

    def set_notifier(self, notifier) -> None:
        self.notifier = notifier

    def _notify(self) -> None:
        if self.notifier is not None:
            try:
                self.notifier()
            except Exception:
                pass

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

    def _build_response_locked(self, state: dict, job: dict, callback: dict | None) -> dict:
        response_id = str(state["next_response_id"])
        state["next_response_id"] += 1
        now_ts = time.time()

        return {
            "response_id": response_id,
            "job_id": str(job.get("job_id", "")),
            "session_id": str(job.get("session_id", "")),
            "node_id": str(job.get("node_id", self.node_id)),
            "command": str(job.get("command", "")),
            "status": str(job.get("status", "")),
            "output": str(job.get("output", "")),
            "cwd": str(job.get("cwd", "")),
            "returncode": job.get("returncode"),
            "error": job.get("error"),
            "created_at": self._utc_now(),
            "ready_at": str(job.get("finished_at", self._utc_now())),
            "delivery_status": "pending",
            "delivery_attempts": 0,
            "last_delivery_attempt_at": None,
            "next_retry_at": now_ts,
            "expires_at_ts": now_ts + self.response_ttl_seconds,
            "delivered_at": None,
            "client_callback": self._normalize_callback(callback),
        }

    def enqueue_command(self, session_id: str, command: str, cwd: str) -> dict:
        with self.store.lock:
            state = self.store.load_state()

            job_id = str(state["next_job_id"])
            state["next_job_id"] += 1

            job = {
                "job_id": job_id,
                "session_id": session_id,
                "node_id": self.node_id,
                "command": command,
                "status": "queued",
                "created_at": self._utc_now(),
                "started_at": None,
                "finished_at": None,
                "output": "",
                "cwd": cwd,
                "returncode": None,
                "error": None,
            }

            state["queue"].append(job)
            self.store.purge_expired_responses_locked(state)
            self.store.save_state(state)

        return {
            "job_id": job_id,
            "session_id": session_id,
            "status": "queued",
            "cwd": cwd,
            "node_id": self.node_id,
        }

    def claim_next_job(self, session_id: str) -> dict | None:
        with self.store.lock:
            state = self.store.load_state()
            self.store.purge_expired_responses_locked(state)

            claimed_job: dict | None = None

            for item in state["queue"]:
                if str(item.get("session_id", "")) != session_id:
                    continue
                if str(item.get("status", "")) != "queued":
                    continue

                item["status"] = "running"
                item["started_at"] = self._utc_now()
                claimed_job = dict(item)
                break

            if claimed_job is not None:
                self.store.save_state(state)

        return claimed_job

    def finish_job(
        self,
        job_id: str,
        status: str,
        output: str,
        cwd: str,
        returncode: int | None,
        error: str | None,
        callback: dict | None,
    ) -> dict | None:
        response_entry: dict | None = None

        with self.store.lock:
            state = self.store.load_state()

            queue_item: dict | None = None
            for item in state["queue"]:
                if str(item.get("job_id", "")) == str(job_id):
                    queue_item = item
                    break

            if queue_item is None:
                return None

            queue_item["status"] = status
            queue_item["finished_at"] = self._utc_now()
            queue_item["output"] = output
            queue_item["cwd"] = cwd
            queue_item["returncode"] = returncode
            queue_item["error"] = error

            final_job = dict(queue_item)

            state["queue"] = [
                item
                for item in state["queue"]
                if str(item.get("job_id", "")) != str(job_id)
            ]
            state["history"].append(final_job)

            response_entry = self._build_response_locked(state, final_job, callback)
            state["responses"].append(response_entry)

            self.store.purge_expired_responses_locked(state)
            self.store.save_state(state)

        self._notify()
        return response_entry

    def get_session_jobs(self, session_id: str) -> dict:
        with self.store.lock:
            state = self.store.load_state()
            changed = self.store.purge_expired_responses_locked(state)

            queue_items = [
                item
                for item in state["queue"]
                if str(item.get("session_id", "")) == session_id
            ]
            history_items = [
                item
                for item in state["history"]
                if str(item.get("session_id", "")) == session_id
            ]
            response_items = [
                item
                for item in state["responses"]
                if str(item.get("session_id", "")) == session_id
            ]

            if changed:
                self.store.save_state(state)

        return {
            "session_id": session_id,
            "queue": queue_items,
            "history": history_items,
            "responses": response_items,
        }

    def cancel_queued_for_session(self, session_id: str, reason: str, callback: dict | None = None) -> None:
        with self.store.lock:
            state = self.store.load_state()
            new_queue: list[dict] = []
            changed = False

            for job in state["queue"]:
                if str(job.get("session_id", "")) != session_id or str(job.get("status", "")) == "running":
                    new_queue.append(job)
                    continue

                job["status"] = "canceled"
                job["finished_at"] = self._utc_now()
                job["output"] = ""
                job["cwd"] = str(job.get("cwd", ""))
                job["returncode"] = None
                job["error"] = reason
                state["history"].append(job)
                state["responses"].append(self._build_response_locked(state, job, callback))
                changed = True

            if changed:
                state["queue"] = new_queue
                self.store.purge_expired_responses_locked(state)
                self.store.save_state(state)

        if changed:
            self._notify()

    def mark_startup_orphans(self) -> None:
        with self.store.lock:
            state = self.store.load_state()

            if not state["queue"]:
                changed = self.store.purge_expired_responses_locked(state)
                if changed:
                    self.store.save_state(state)
                return

            pending = list(state["queue"])
            state["queue"] = []

            for job in pending:
                job["status"] = "orphaned"
                job["started_at"] = job.get("started_at") or self._utc_now()
                job["finished_at"] = self._utc_now()
                job["output"] = ""
                job["cwd"] = str(job.get("cwd", ""))
                job["returncode"] = None
                job["error"] = "server restarted before queued command could finish"
                state["history"].append(job)
                state["responses"].append(self._build_response_locked(state, job, None))

            self.store.purge_expired_responses_locked(state)
            self.store.save_state(state)

        self._notify()

    def update_callback_for_session(self, session_id: str, callback: dict | None) -> None:
        callback_info = self._normalize_callback(callback)
        if callback_info is None:
            return

        with self.store.lock:
            state = self.store.load_state()
            changed = False

            for item in state["responses"]:
                if str(item.get("session_id", "")) != session_id:
                    continue
                if str(item.get("delivery_status", "")) == "delivered":
                    continue

                item["client_callback"] = callback_info
                if item.get("next_retry_at") is None:
                    item["next_retry_at"] = time.time()
                changed = True

            if changed:
                self.store.save_state(state)

        if changed:
            self._notify()

    def get_due_responses(self, now_ts: float | None = None) -> list[dict]:
        if now_ts is None:
            now_ts = time.time()

        with self.store.lock:
            state = self.store.load_state()
            changed = self.store.purge_expired_responses_locked(state)

            result = []
            for item in state["responses"]:
                if str(item.get("delivery_status", "")) == "delivered":
                    continue
                callback = item.get("client_callback")
                if not isinstance(callback, dict):
                    continue

                next_retry_at = item.get("next_retry_at")
                if next_retry_at is None:
                    continue

                try:
                    if float(next_retry_at) <= now_ts:
                        result.append(dict(item))
                except Exception:
                    continue

            if changed:
                self.store.save_state(state)

        return result

    def mark_delivery_success(self, response_id: str) -> bool:
        removed = False

        with self.store.lock:
            state = self.store.load_state()
            new_responses = []

            for item in state["responses"]:
                if str(item.get("response_id", "")) == response_id:
                    removed = True
                    continue
                new_responses.append(item)

            state["responses"] = new_responses
            self.store.purge_expired_responses_locked(state)
            self.store.save_state(state)

        return removed

    def mark_delivery_failed(self, response_id: str) -> bool:
        changed = False
        now_ts = time.time()

        with self.store.lock:
            state = self.store.load_state()

            for item in state["responses"]:
                if str(item.get("response_id", "")) != response_id:
                    continue

                attempts = int(item.get("delivery_attempts", 0)) + 1
                item["delivery_attempts"] = attempts
                item["last_delivery_attempt_at"] = now_ts
                item["delivery_status"] = "pending"

                if attempts < len(self.retry_schedule_seconds):
                    item["next_retry_at"] = now_ts + self.retry_schedule_seconds[attempts]
                else:
                    item["next_retry_at"] = None

                changed = True
                break

            if changed:
                self.store.purge_expired_responses_locked(state)
                self.store.save_state(state)

        return changed

    def list_pending_responses(self, session_id: str | None = None) -> list[dict]:
        with self.store.lock:
            state = self.store.load_state()
            changed = self.store.purge_expired_responses_locked(state)

            result = []
            for item in state["responses"]:
                if str(item.get("delivery_status", "")) == "delivered":
                    continue
                if session_id and str(item.get("session_id", "")) != session_id:
                    continue
                result.append(dict(item))

            if changed:
                self.store.save_state(state)

        return result

    def ack_responses(self, response_ids: list[str]) -> int:
        ids = {str(item) for item in response_ids if str(item).strip()}
        if not ids:
            return 0

        removed = 0
        with self.store.lock:
            state = self.store.load_state()
            new_responses = []

            for item in state["responses"]:
                if str(item.get("response_id", "")) in ids:
                    removed += 1
                    continue
                new_responses.append(item)

            state["responses"] = new_responses
            self.store.purge_expired_responses_locked(state)
            self.store.save_state(state)

        return removed