from __future__ import annotations

import threading
import time


class CleanupService:
    def __init__(self, sessions, jobs, store) -> None:
        self.sessions = sessions
        self.jobs = jobs
        self.store = store

    def start(self) -> None:
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

    def _cleanup_expired_sessions_loop(self) -> None:
        while True:
            time.sleep(self.sessions.session_cleaner_interval)

            expired = self.sessions.pop_expired_sessions()
            for session_id, session in expired:
                try:
                    callback = session.get("client_callback")
                    self.jobs.cancel_queued_for_session(
                        session_id,
                        "session expired by inactivity timeout",
                        callback=callback,
                    )
                    self.sessions.close_session_resources_static(session)
                    print(f"SESSION TIMEOUT: closed inactive session {session_id}")
                except Exception as e:
                    print(f"SESSION TIMEOUT ERROR: {session_id}: {e}")

    def _cleanup_expired_responses_loop(self) -> None:
        while True:
            time.sleep(60)

            with self.store.lock:
                state = self.store.load_state()
                changed = self.store.purge_expired_responses_locked(state)
                if changed:
                    self.store.save_state(state)