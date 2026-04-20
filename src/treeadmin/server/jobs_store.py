from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path


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
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    def _ensure_state_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._atomic_write_json(self.path, self._default_state())

    def load_state(self) -> dict:
        self._ensure_state_file()
        raw = self.path.read_text(encoding="utf-8").strip()

        if not raw:
            return self._default_state()

        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("server_jobs.json root must be object")

        result = self._default_state()
        result.update(data)

        if not isinstance(result["queue"], list):
            result["queue"] = []
        if not isinstance(result["history"], list):
            result["history"] = []
        if not isinstance(result["responses"], list):
            result["responses"] = []

        try:
            result["next_job_id"] = int(result["next_job_id"])
        except Exception:
            result["next_job_id"] = 1

        try:
            result["next_response_id"] = int(result["next_response_id"])
        except Exception:
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
        return len(state["responses"]) != before