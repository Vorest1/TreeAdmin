from __future__ import annotations

import time
from datetime import datetime


class JobService:
    def __init__(
        self,
        store,
        node_id: str,
        response_ttl_seconds: int = 48 * 60 * 60,
    ) -> None:
        self.store = store
        self.node_id = node_id
        self.response_ttl_seconds = response_ttl_seconds

    @staticmethod
    def _utc_now() -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"

    def _build_response_locked(self, state: dict, job: dict) -> dict:
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
            "expires_at_ts": now_ts + self.response_ttl_seconds,
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

            response_entry = self._build_response_locked(state, final_job)
            state["responses"].append(response_entry)

            self.store.purge_expired_responses_locked(state)
            self.store.save_state(state)

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

    def cancel_queued_for_session(self, session_id: str, reason: str) -> None:
        changed = False

        with self.store.lock:
            state = self.store.load_state()
            new_queue: list[dict] = []

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
                state["responses"].append(self._build_response_locked(state, job))
                changed = True

            if changed:
                state["queue"] = new_queue
                self.store.purge_expired_responses_locked(state)
                self.store.save_state(state)

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
                state["responses"].append(self._build_response_locked(state, job))

            self.store.purge_expired_responses_locked(state)
            self.store.save_state(state)

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