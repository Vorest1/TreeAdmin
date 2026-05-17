from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("treeadmin.audit")

class SessionWorkers:
    def __init__(self, sessions, jobs) -> None:
        self.sessions = sessions
        self.jobs = jobs
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

        # log
        logger.debug("session worker Initialized")
        #

    def ensure_worker(self, session_id: str) -> None:
        with self._lock:
            thread = self._workers.get(session_id)
            if thread is not None and thread.is_alive():
                # log
                logger.debug(
                    "session worker already running : session_id=%s, thread_name=%s",
                    session_id,
                    thread.name
                )
                #
                return

            worker = threading.Thread(
                target=self._worker_loop,
                args=(session_id,),
                daemon=True,
                name=f"session-worker-{session_id}",
            )
            self._workers[session_id] = worker
            #worker.start()
            # log
            try:
                worker.start()
            except Exception:
                self._workers.pop(session_id, None)
                logger.exception(
                    "session worker start failed : session_id=%s thread_name=%s",
                    session_id,
                    worker.name,
                )
                raise

            logger.info(
                "session worker started : session_id=%s thread_name=%s",
                session_id,
                worker.name,
            )
            #

    def _worker_loop(self, session_id: str) -> None:
        try:
            while True:
                session = self.sessions.get_session(session_id)
                if session is None:
                    return

                claimed_job = self.jobs.claim_next_job(session_id)
                if claimed_job is None:
                    time.sleep(0.5)
                    continue

                try:
                    output, cwd, returncode = self.sessions.execute(
                        session_id,
                        str(claimed_job.get("command", "")),
                    )
                    status = "finished"
                    error = None
                except Exception as e:
                    output, cwd, returncode = "", "", None
                    status = "failed"
                    error = str(e)

                self.jobs.finish_job(
                    job_id=str(claimed_job.get("job_id", "")),
                    status=status,
                    output=output,
                    cwd=cwd,
                    returncode=returncode,
                    error=error,
                )

                # log
                logger.info(
                    "session worker finished : command=[%s]",
                    str(claimed_job.get("command", ""))
                )
                #
        finally:
            with self._lock:
                # log
                logger.info(
                    "session worker stopped : session_id=%s thread_name=%s",
                    session_id,
                    self._workers[session_id]
                )
                #
                self._workers.pop(session_id, None)