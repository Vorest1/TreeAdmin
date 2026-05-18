from __future__ import annotations
from datetime import datetime
from typing import Any

import time
import logging

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("treeadmin.audit")

class JobsManager:
    def __init__(
        self,
        store,
        node_id: str,
        response_ttl_seconds: int = 48 * 60 * 60,
    ) -> None:
        self.store = store
        self.node_id = node_id
        self.response_ttl_seconds = response_ttl_seconds

        # log
        logger.info(
            "Jobs manager Initialized : node_id=%s",
            node_id
        )
        #

    @staticmethod
    def _utc_now() -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"

    @staticmethod
    def _is_failed_response(item: dict[str, Any]) -> bool:
        status = str(item.get("status", "")).lower()
        error = item.get("error")
        returncode = item.get("returncode")

        if status in {"failed", "canceled", "orphaned", "interrupted", "lost", "expired"}:
            return True

        if error:
            return True

        if isinstance(returncode, int) and returncode != 0:
            return True

        return False

    @staticmethod
    def _short_error(value: Any, limit: int = 180) -> str | None:
        if value is None:
            return None

        text = str(value).strip()
        if not text:
            return None

        if len(text) <= limit:
            return text

        return text[: limit - 3] + "..."

    @staticmethod
    def _output_size(value: Any) -> int:
        if value is None:
            return 0

        return len(str(value).encode("utf-8", errors="replace"))

    def _build_response_locked(self, state: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
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
            "output_size": int(job.get("output_size", self._output_size(job.get("output", ""))) or 0),
            "output_truncated": bool(job.get("output_truncated", False)),
            "original_output_size": int(job.get("original_output_size", 0) or 0),
        }

    def _build_history_entry(self, job: dict[str, Any]) -> dict[str, Any]:
        # Full output is stored only in pending responses.
        # History keeps only a short metadata record.
        return {
            "job_id": str(job.get("job_id", "")),
            "session_id": str(job.get("session_id", "")),
            "node_id": str(job.get("node_id", self.node_id)),
            "command": str(job.get("command", "")),
            "status": str(job.get("status", "")),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "cwd": str(job.get("cwd", "")),
            "returncode": job.get("returncode"),
            "error": job.get("error"),
            "output_size": int(job.get("output_size", 0) or 0),
            "output_truncated": bool(job.get("output_truncated", False)),
            "original_output_size": int(job.get("original_output_size", 0) or 0),
        }

    def _response_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "response_id": str(item.get("response_id", "")),
            "job_id": str(item.get("job_id", "")),
            "session_id": str(item.get("session_id", "")),
            "node_id": str(item.get("node_id", self.node_id)),
            "command": str(item.get("command", "")),
            "status": str(item.get("status", "")),
            "returncode": item.get("returncode"),
            "ready_at": item.get("ready_at") or item.get("created_at"),
            "created_at": item.get("created_at"),
            "cwd": str(item.get("cwd", "")),
            "output_size": int(item.get("output_size", self._output_size(item.get("output", ""))) or 0),
            "output_truncated": bool(item.get("output_truncated", False)),
            "has_error": self._is_failed_response(item),
            "error": self._short_error(item.get("error")),
        }

    def _queue_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(item.get("job_id", "")),
            "session_id": str(item.get("session_id", "")),
            "node_id": str(item.get("node_id", self.node_id)),
            "command": str(item.get("command", "")),
            "status": str(item.get("status", "")),
            "created_at": item.get("created_at"),
            "started_at": item.get("started_at"),
            "cwd": str(item.get("cwd", "")),
        }

    def enqueue_command(self, session_id: str, command: str, cwd: str) -> dict[str, Any]:
        with self.store.lock:
            state = self.store.load_state()
            self.store.purge_expired_responses_locked(state)

            queue_count = len(state["queue"]) # for log
            if self.store.max_queue_items > 0 and queue_count >= self.store.max_queue_items:
                # log
                logger.warning(
                    "job queue limit exceeded : session_id=%s queue_count=%s max_queue_items=%s command=[%s]",
                    session_id,
                    queue_count,
                    self.store.max_queue_items,
                    command,
                )
                #
                raise RuntimeError(
                    f"server job queue limit exceeded: {queue_count}/{self.store.max_queue_items}"
                )

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
                "output_size": 0,
                "output_truncated": False,
                "original_output_size": 0,
            }

            state["queue"].append(job)
            stats = state.setdefault("stats", {})
            stats["total_jobs_created"] = int(stats.get("total_jobs_created", 0) or 0) + 1

            self.store.save_state(
                state,
                journal_op="enqueue",
                journal_payload={
                    "job_id": job_id,
                    "session_id": session_id,
                    "node_id": self.node_id,
                },
            )

        # log
        logger.info(
            "job queued : job_id=%s session_id=%s command=[%s]",
            job_id,
            session_id,
            command
        )
        audit_logger.info(
            "job queued : job_id=%s session_id=%s node_id=%s command=[%s]",
            job_id,
            session_id,
            self.node_id,
            command
        )
        #

        return {
            "job_id": job_id,
            "session_id": session_id,
            "status": "queued",
            "cwd": cwd,
            "node_id": self.node_id,
        }

    def claim_next_job(self, session_id: str) -> dict[str, Any] | None:
        with self.store.lock:
            state = self.store.load_state()
            changed = self.store.purge_expired_responses_locked(state)

            claimed_job: dict[str, Any] | None = None

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
                self.store.save_state(
                    state,
                    journal_op="claim",
                    journal_payload={
                        "job_id": str(claimed_job.get("job_id", "")),
                        "session_id": session_id,
                    },
                )
            elif changed:
                self.store.save_state(state, journal_op="cleanup_expired_responses")
        
        # log
        if claimed_job is not None:
            logger.debug(
                "job claimed by manager : job_id=%s session_id=%s node_id=%s",
                claimed_job.get("job_id"),
                session_id,
                self.node_id
            )
        #

        return claimed_job

    def finish_job(
        self,
        job_id: str,
        status: str,
        output: str,
        cwd: str,
        returncode: int | None,
        error: str | None,
    ) -> dict[str, Any] | None:
        response_entry: dict[str, Any] | None = None
        final_job: dict[str, Any] | None = None

        output_for_response, original_output_size, output_truncated = self.store.truncate_output(output)
        response_output_size = self._output_size(output_for_response)

        with self.store.lock:
            state = self.store.load_state()

            queue_item: dict[str, Any] | None = None
            for item in state["queue"]:
                if str(item.get("job_id", "")) == str(job_id):
                    queue_item = item
                    break

            if queue_item is None:
                # log
                logger.warning(
                    "job finish skipped - job not found : job_id=%s status=%s returncode=%s",
                    job_id,
                    status,
                    returncode
                )
                #
                return None

            queue_item["status"] = status
            queue_item["finished_at"] = self._utc_now()
            queue_item["output"] = output_for_response
            queue_item["cwd"] = cwd
            queue_item["returncode"] = returncode
            queue_item["error"] = error
            queue_item["output_size"] = response_output_size
            queue_item["output_truncated"] = output_truncated
            queue_item["original_output_size"] = original_output_size

            final_job = dict(queue_item)

            state["queue"] = [
                item
                for item in state["queue"]
                if str(item.get("job_id", "")) != str(job_id)
            ]
            state["history"].append(self._build_history_entry(final_job))

            response_entry = self._build_response_locked(state, final_job)
            state["responses"].append(response_entry)

            stats = state.setdefault("stats", {})
            stats["total_jobs_finished"] = int(stats.get("total_jobs_finished", 0) or 0) + 1
            if status in {"failed", "canceled", "orphaned", "interrupted", "lost"} or error:
                stats["total_jobs_failed"] = int(stats.get("total_jobs_failed", 0) or 0) + 1

            self.store.purge_expired_responses_locked(state)
            self.store.save_state(
                state,
                journal_op="finish",
                journal_payload={
                    "job_id": job_id,
                    "response_id": str(response_entry.get("response_id", "")),
                    "session_id": str(final_job.get("session_id", "")),
                    "status": status,
                    "returncode": returncode,
                    "output_size": response_output_size,
                    "output_truncated": output_truncated,
                },
            )

        # for log
        response_id = str(response_entry.get("response_id", "")) if response_entry else ""
        session_id = str(final_job.get("session_id", "")) if final_job else ""
        command = str(final_job.get("command", "")) if final_job else ""
        #

        # log
        log_message = (
            "job finished : job_id=%s response_id=%s session_id=%s  "
            "command=[%s] status=%s"
        )
        log_args = (
            job_id,
            response_id,
            session_id,
            command,
            status
        )

        if status in {"failed", "canceled", "orphaned", "interrupted", "lost"} or error:
            logger.warning(log_message, *log_args)
        else:
            logger.info(log_message, *log_args)

        audit_logger.info(
            "job finished : job_id=%s response_id=%s session_id=%s node_id=%s "
            "command=[%s] status=%s returncode=%s output_size=%s output_truncated=%s",
            job_id,
            response_id,
            session_id,
            self.node_id,
            command,
            status,
            returncode,
            response_output_size,
            output_truncated,
        )
        #

        return response_entry

    def get_session_jobs(self, session_id: str) -> dict[str, Any]:
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
                self.store.save_state(state, journal_op="cleanup_expired_responses")

        # log
        logger.debug(
            "session jobs requested : session_id=%s queue_count=%s history_count=%s "
            "responses_count=%s purged_expired_responses=%s",
            session_id,
            len(queue_items),
            len(history_items),
            len(response_items),
            changed,
        )
        #

        return {
            "session_id": session_id,
            "queue": queue_items,
            "history": history_items,
            "responses": response_items,
        }

    def get_results_summary(self, session_id: str | None = None) -> dict[str, Any]:
        with self.store.lock:
            state = self.store.load_state()
            changed = self.store.purge_expired_responses_locked(state)

            queue_items = []
            for item in state["queue"]:
                if session_id and str(item.get("session_id", "")) != session_id:
                    continue
                queue_items.append(dict(item))

            response_items = []
            for item in state["responses"]:
                if session_id and str(item.get("session_id", "")) != session_id:
                    continue
                response_items.append(dict(item))

            if changed:
                self.store.save_state(state, journal_op="cleanup_expired_responses")

        queued = [
            self._queue_summary(item)
            for item in queue_items
            if str(item.get("status", "")) == "queued"
        ]

        running = [
            self._queue_summary(item)
            for item in queue_items
            if str(item.get("status", "")) == "running"
        ]

        responses = [self._response_summary(item) for item in response_items]

        failed = [item for item in responses if item.get("has_error")]
        finished = [item for item in responses if not item.get("has_error")]

        # log
        logger.debug(
            "results summary requested : node_id=%s session_id=%s ready_count=%s "
            "finished_count=%s failed_count=%s queued_count=%s running_count=%s "
            "purged_expired_responses=%s",
            self.node_id,
            session_id,
            len(responses),
            len(finished),
            len(failed),
            len(queued),
            len(running),
            changed,
        )
        #

        return {
            "node_id": self.node_id,
            "session_id": session_id,
            "ready_count": len(responses),
            "finished_count": len(finished),
            "failed_count": len(failed),
            "queued_count": len(queued),
            "running_count": len(running),
            "responses": responses,
            "queued": queued,
            "running": running,
        }

    def cancel_queued_for_session(self, session_id: str, reason: str) -> None:
        changed = False
        canceled_count = 0 # for log

        with self.store.lock:
            state = self.store.load_state()
            new_queue: list[dict[str, Any]] = []

            for job in state["queue"]:
                if (
                    str(job.get("session_id", "")) != session_id
                    or str(job.get("status", "")) == "running"
                ):
                    new_queue.append(job)
                    continue

                job["status"] = "canceled"
                job["finished_at"] = self._utc_now()
                job["output"] = ""
                job["cwd"] = str(job.get("cwd", ""))
                job["returncode"] = None
                job["error"] = reason
                job["output_size"] = 0
                job["output_truncated"] = False
                job["original_output_size"] = 0

                state["history"].append(self._build_history_entry(job))
                state["responses"].append(self._build_response_locked(state, job))
                changed = True
                canceled_count += 1 # for log

            if changed:
                state["queue"] = new_queue
                self.store.purge_expired_responses_locked(state)
                self.store.save_state(
                    state,
                    journal_op="cancel_queued_for_session",
                    journal_payload={
                        "session_id": session_id,
                        "reason": reason,
                        "count": canceled_count,
                    },
                )
            # log
            queue_count = len(state["queue"])
            history_count = len(state["history"])
            responses_count = len(state["responses"])
            #
        # log
        if changed:
            logger.warning(
                "queued jobs canceled : session_id=%s reason=%s canceled_count=%s "
                "queue_count=%s history_count=%s responses_count=%s",
                session_id,
                reason,
                canceled_count,
                queue_count,
                history_count,
                responses_count,
            )

            audit_logger.info(
                "queued jobs canceled : session_id=%s reason=%s canceled_count=%s",
                session_id,
                reason,
                canceled_count,
            )
        else:
            logger.debug(
                "queued jobs cancel skipped no matching jobs : session_id=%s reason=%s",
                session_id,
                reason,
            )
        #

    def recover_after_startup(self) -> None:
        with self.store.lock:
            state = self.store.load_state()
            now = self._utc_now()
            recovered_running = 0 # for log
            kept_queued = 0 # for log
            new_queue: list[dict[str, Any]] = []

            for job in state["queue"]:
                status = str(job.get("status", ""))

                if status == "queued":
                    kept_queued += 1 # for log
                    new_queue.append(job)
                    continue

                if status == "running":
                    job["status"] = "interrupted"
                    job["finished_at"] = now
                    job["output"] = ""
                    job["cwd"] = str(job.get("cwd", ""))
                    job["returncode"] = None
                    job["error"] = "server restarted while command was running"
                    job["output_size"] = 0
                    job["output_truncated"] = False
                    job["original_output_size"] = 0
                    state["history"].append(self._build_history_entry(job))
                    state["responses"].append(self._build_response_locked(state, job))
                    recovered_running += 1 # for log
                    continue

                job["status"] = "interrupted"
                job["finished_at"] = now
                job["output"] = ""
                job["cwd"] = str(job.get("cwd", ""))
                job["returncode"] = None
                job["error"] = f"server restarted with unsupported queued status: {status}"
                job["output_size"] = 0
                job["output_truncated"] = False
                job["original_output_size"] = 0
                state["history"].append(self._build_history_entry(job))
                state["responses"].append(self._build_response_locked(state, job))
                recovered_running += 1 # for log

            state["queue"] = new_queue
            state.setdefault("stats", {})["last_recovery_at"] = now
            if recovered_running:
                stats = state.setdefault("stats", {})
                stats["total_jobs_failed"] = int(stats.get("total_jobs_failed", 0) or 0) + recovered_running

            changed = self.store.purge_expired_responses_locked(state)
            changed = self.store.compact_state_locked(state) or changed or recovered_running > 0
            changed = True # last_recovery_at was updated

            if changed:
                self.store.save_state(
                    state,
                    journal_op="startup_recovery",
                    journal_payload={
                        "kept_queued": kept_queued,
                        "interrupted": recovered_running,
                    },
                )

            # for log
            history_count = len(state["history"])
            responses_count = len(state["responses"])
            queue_count = len(state["queue"])
            #

        # log
        if recovered_running:
            logger.warning(
                "startup recovery interrupted running jobs : node_id=%s count=%s kept_queued=%s "
                "queue_count=%s history_count=%s responses_count=%s",
                self.node_id,
                recovered_running,
                kept_queued,
                queue_count,
                history_count,
                responses_count,
            )

            audit_logger.info(
                "startup recovery interrupted running jobs : node_id=%s count=%s kept_queued=%s",
                self.node_id,
                recovered_running,
                kept_queued,
            )
        else:
            logger.info(
                "startup recovery done : node_id=%s kept_queued=%s queue_count=%s",
                self.node_id,
                kept_queued,
                queue_count,
            )
        #

    def mark_startup_orphans(self) -> None:
        # Backward-compatible method name.
        # New behavior is safer for real use: queued jobs stay queued,
        # running jobs are reported as interrupted.
        self.recover_after_startup()

    def list_pending_responses(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.lock:
            state = self.store.load_state()
            changed = self.store.purge_expired_responses_locked(state)

            result = []
            for item in state["responses"]:
                if session_id and str(item.get("session_id", "")) != session_id:
                    continue
                result.append(dict(item))

            if changed:
                self.store.save_state(state, journal_op="cleanup_expired_responses")
        
        # log
        logger.debug(
            "pending responses listed : node_id=%s session_id=%s count=%s "
            "purged_expired_responses=%s",
            self.node_id,
            session_id,
            len(result),
            changed,
        )
        #
        return result

    def ack_responses(self, response_ids: list[str]) -> int:
        ids = {str(item) for item in response_ids if str(item).strip()}
        if not ids:
            logger.debug("ack responses skipped empty ids")
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
            self.store.save_state(
                state,
                journal_op="ack",
                journal_payload={
                    "requested_count": len(ids),
                    "removed_count": removed,
                    "response_ids": sorted(ids),
                },
            )
            responses_count = len(state["responses"]) # for log

        # log
        if removed:
            logger.info(
                "responses acknowledged : requested_count=%s removed_count=%s "
                "remaining_responses_count=%s",
                len(ids),
                removed,
                responses_count,
            )

            audit_logger.info(
                "responses acknowledged : requested_count=%s removed_count=%s",
                len(ids),
                removed,
            )
        else:
            logger.warning(
                "responses acknowledge no matches : requested_count=%s remaining_responses_count=%s",
                len(ids),
                responses_count,
            )
        #
        return removed
