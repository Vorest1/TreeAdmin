import logging
import threading
import time

logger = logging.getLogger(__name__)


class CleanupService:
    def __init__(self, sessions, jobs, store):
        self.sessions = sessions
        self.jobs = jobs
        self.store = store

    def start(self):
        # type: () -> None
        session_thread = threading.Thread(
            target=self._cleanup_expired_sessions_loop,
            daemon=True,
            name="cleanup-sessions",
        )
        session_thread.start()

        response_thread = threading.Thread(
            target=self._cleanup_expired_responses_loop,
            daemon=True,
            name="cleanup-responses",
        )
        response_thread.start()
        # log
        logger.info(
            "Cleanup Service started : session_thread=%s response_thread=%s "
            "session_cleaner_interval=%s",
            session_thread.name,
            response_thread.name,
            self.sessions.session_cleaner_interval
        )
        #

    def _cleanup_expired_sessions_loop(self):
        # type: () -> None
        while True:
            time.sleep(self.sessions.session_cleaner_interval)

            try:
                expired = self.sessions.pop_expired_sessions()
            except Exception:
                logger.exception("Cleanup expired sessions scan failed")
                continue

            for session_id, session in expired:
                try:
                    self.jobs.cancel_queued_for_session(
                        session_id,
                        "session expired by inactivity timeout",
                    )
                    self.sessions.close_session_resources_static(session)
                    
                    # log
                    logger.info(
                        "Cleanup expired session closed : session_id=%s platform=%s cwd=%s",
                        session_id,
                        session.get("platform"),
                        session.get("cwd"),
                    )
                    #

                    print("SESSION TIMEOUT: closed inactive session {}".format(session_id))
                except Exception as e:
                    # log
                    logger.exception(
                        "Cleanup expired session close failed : session_id=%s",
                        session_id
                    )
                    #
                    print("SESSION TIMEOUT ERROR: {}: {}".format(session_id, e))

    def _cleanup_expired_responses_loop(self):
        # type: () -> None
        while True:
            time.sleep(60)

            try:
                with self.store.lock:
                    state = self.store.load_state()
                    before_count = len(state.get("responses", []))
                    changed = self.store.purge_expired_responses_locked(state)
                    after_count = len(state.get("responses", []))

                    if changed:
                        self.store.save_state(state)
                        # log
                        logger.info(
                            "Cleanup expired responses purged : before_count=%s "
                            "after_count=%s removed_count=%s",
                            before_count,
                            after_count,
                            before_count - after_count,
                        )
                        #
            except Exception:
                # log
                logger.exception("Cleanup expired responses failed")
                #