from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

class JobsStore:
    def __init__(self, path: Path, response_ttl_seconds: int = 48 * 60 * 60) -> None:
        self.path = Path(path)
        self.response_ttl_seconds = response_ttl_seconds
        self.lock = threading.RLock()
        self._ensure_state_file()

    def _default_state(self) -> dict:
        return {
            "next_job_id": 1,
            "next_response_id": 1,
            "queue": [],
            "history": [],
            "responses": [],
        }

    def _atomic_write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except Exception:
            # log
            logger.exception(
                "Jobs Store write failed : path=%s tmp_path=%s",
                path,
                tmp_path
            ) 
            raise
            #
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                # log
                logger.debug(
                    "Jobs Store tmp cleanup failed : tmp_path=%s",
                    tmp_path,
                    exc_info=True
                )
                #
                #pass

    def _ensure_state_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._atomic_write_json(self.path, self._default_state())
            # log
            logger.info("jobs_store_state_file_created path=%s", self.path)
            #

    def load_state(self) -> dict:
        self._ensure_state_file()
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except Exception:
            # log
            logger.exception("Jobs Store read failed : path=%s", self.path)
            #
            raise
    
        if not raw:
            # log
            logger.warning(
                "Jobs Store empty state file : path=%s default_state_used=True",
                self.path
            )
            #
            return self._default_state()

        try:
            data = json.loads(raw)
        except Exception:
            # log
            logger.exception(
                "Jobs Store json decode failed : path=%s",
                self.path
            )
            #
            raise

        if not isinstance(data, dict):
            # log
            logger.error(
                "Jobs Store invalid root : path=%s root_type=%s",
                self.path,
                type(data).__name__
            )
            #
            raise ValueError("server_jobs.json root must be object")

        result = self._default_state()
        result.update(data)

        if not isinstance(result["queue"], list):
            # log
            logger.warning(
                "Jobs Store invalid queue reset : path=%s value_type=%s",
                self.path,
                type(result["queue"]).__name__
            )
            #
            result["queue"] = []
        if not isinstance(result["history"], list):
            # log
            logger.warning(
                "Jobs Store invalid history reset : path=%s value_type=%s",
                self.path,
                type(result["queue"]).__name__
            )
            #
            result["history"] = []
        if not isinstance(result["responses"], list):
            # log
            logger.warning(
                "Jobs Store invalid responses reset : path=%s value_type=%s",
                self.path,
                type(result["queue"]).__name__
            )
            #
            result["responses"] = []

        try:
            result["next_job_id"] = int(result["next_job_id"])
        except Exception:
            # log
            logger.warning(
                "Jobs Store invalid next_job_id reset : path=%s",
                self.path,
                exc_info=True
            )
            #
            result["next_job_id"] = 1

        try:
            result["next_response_id"] = int(result["next_response_id"])
        except Exception:
            # log
            logger.warning(
                "Jobs Store invalid next_response_id reset path=%s",
                self.path,
                exc_info=True
            )
            #
            result["next_response_id"] = 1

        return result

    def save_state(self, state: dict) -> None:
        self._atomic_write_json(self.path, state)

    def purge_expired_responses_locked(self, state: dict) -> bool:
        now = time.time()
        before = len(state["responses"])
        state["responses"] = [
            item
            for item in state["responses"]
            if float(item.get("expires_at_ts", now + self.response_ttl_seconds)) > now
        ]

        after = len(state["responses"])
        changed = after != before

        # log
        if changed:
            logger.info(
                "Jobs Store expired responses purged : before_count=%s after_count=%s removed_count=%s",
                before,
                after,
                before - after
            )
        #

        return changed