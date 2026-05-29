import base64
import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger(__name__)


class JobsStore:
    SCHEMA_VERSION = 1

    def __init__(
        self,
        path,
        response_ttl_seconds=48 * 60 * 60,
        max_output_bytes=None,
        max_history_items=None,
        max_responses_items=None,
        max_queue_items=None,
    ):
        # type: (Path, int, Optional[int], Optional[int], Optional[int], Optional[int]) -> None
        self.path = Path(path)
        self.response_ttl_seconds = response_ttl_seconds

        self.max_output_bytes = self._env_int(
            "TREEADMIN_MAX_OUTPUT_BYTES",
            max_output_bytes if max_output_bytes is not None else 512 * 1024,
        )
        self.max_history_items = self._env_int(
            "TREEADMIN_MAX_HISTORY_ITEMS",
            max_history_items if max_history_items is not None else 200,
        )
        self.max_responses_items = self._env_int(
            "TREEADMIN_MAX_RESPONSES_ITEMS",
            max_responses_items if max_responses_items is not None else 100,
        )
        self.max_queue_items = self._env_int(
            "TREEADMIN_MAX_QUEUE_ITEMS",
            max_queue_items if max_queue_items is not None else 100,
        )

        self.lock = threading.RLock()

        self.backup_path = self.path.with_name("{}.bak".format(self.path.name))
        self.journal_path = self.path.parent / "journal.jsonl"
        self.lock_path = self.path.parent / "lock"
        self.tmp_dir = self.path.parent / "tmp"

        self._process_lock_file = None

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        self._acquire_process_lock()
        self._migrate_legacy_state_if_needed()
        self._ensure_state_file()

        # log
        logger.info(
            "Jobs Store Initialized : path=%s backup_path=%s journal_path=%s "
            "max_output_bytes=%s max_history_items=%s max_responses_items=%s "
            "max_queue_items=%s",
            self.path,
            self.backup_path,
            self.journal_path,
            self.max_output_bytes,
            self.max_history_items,
            self.max_responses_items,
            self.max_queue_items,
        )
        #

    @staticmethod
    def _env_int(name, default):
        # type: (str, int) -> int
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default

        try:
            value = int(raw)
        except ValueError:
            # log
            logger.warning(
                "Jobs Store invalid integer env value ignored : name=%s value=%s",
                name,
                raw,
            )
            #
            return default

        return max(0, value)

    @staticmethod
    def _utc_now():
        # type: () -> str
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def normalize_output_storage_format(value, default="base64"):
        # type: (Any, str) -> str
        normalized = str(value or default).strip().lower()

        if normalized in {"base64", "b64"}:
            return "base64"

        if normalized in {"text", "plain", "human", "readable"}:
            return "text"

        logger.warning(
            "Jobs Store invalid output storage format ignored : value=%s default=%s",
            value,
            default,
        )
        return default

    def prepare_output_for_storage(
        self,
        value,
        output_storage_format="base64",
    ):
        # type: (Any, Any) -> Dict[str, Any]
        output_text, original_output_size, output_truncated = self.truncate_output(value)
        output_raw = output_text.encode("utf-8", errors="replace")

        normalized_format = self.normalize_output_storage_format(
            output_storage_format,
            default="base64",
        )

        if normalized_format == "base64":
            stored_output = base64.b64encode(output_raw).decode("ascii")
            output_encoding = "base64"
            stored_output_size = len(stored_output.encode("ascii"))
        else:
            stored_output = output_text
            output_encoding = "text"
            stored_output_size = len(output_raw)

        return {
            "output": stored_output,
            "output_encoding": output_encoding,
            "output_size": len(output_raw),
            "stored_output_size": stored_output_size,
            "output_truncated": output_truncated,
            "original_output_size": original_output_size,
        }

    def _default_state(self):
        # type: () -> Dict[str, Any]
        now = self._utc_now()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "store_id": "treeadmin-server-store",
            "created_at": now,
            "updated_at": now,
            "next_job_id": 1,
            "next_response_id": 1,
            "queue": [],
            "history": [],
            "responses": [],
            "stats": {
                "total_jobs_created": 0,
                "total_jobs_finished": 0,
                "total_jobs_failed": 0,
                "last_recovery_at": None,
            },
        }

    def _acquire_process_lock(self):
        # type: () -> None
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._process_lock_file = open(self.lock_path, "a+", encoding="utf-8")

        if os.name != "posix":
            # log
            logger.debug(
                "Jobs Store process lock skipped on non-posix os : path=%s",
                self.lock_path,
            )
            #
            return

        try:
            import fcntl

            fcntl.flock(
                self._process_lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            self._process_lock_file.seek(0)
            self._process_lock_file.truncate()
            self._process_lock_file.write(str(os.getpid()))
            self._process_lock_file.flush()
            os.fsync(self._process_lock_file.fileno())

            # log
            logger.info(
                "Jobs Store process lock acquired : path=%s pid=%s",
                self.lock_path,
                os.getpid(),
            )
            #
        except BlockingIOError:
            # log
            logger.error(
                "Jobs Store process lock failed [already locked] : path=%s",
                self.lock_path,
            )
            #
            raise RuntimeError(
                "Jobs store is already locked: {}. "
                "Another TreeAdmin server instance may be running.".format(self.lock_path)
            )
        except Exception:
            # log
            logger.exception(
                "Jobs Store process lock failed : path=%s",
                self.lock_path,
            )
            #
            raise

    def _migrate_legacy_state_if_needed(self):
        # type: () -> None
        if self.path.exists():
            return

        legacy_path = self.path.parent.parent / "server_jobs.json"
        if not legacy_path.exists():
            return

        try:
            raw = legacy_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else self._default_state()
            normalized = self._normalize_state(data)
            self._atomic_write_json(self.path, normalized, create_backup=False)

            # log
            logger.warning(
                "Jobs Store legacy state migrated : legacy_path=%s new_path=%s",
                legacy_path,
                self.path,
            )
            #
        except Exception:
            # log
            logger.exception(
                "Jobs Store legacy state migration failed : legacy_path=%s new_path=%s",
                legacy_path,
                self.path,
            )
            #
            raise

    def _fsync_dir(self, path):
        # type: (Path) -> None
        if os.name != "posix":
            return

        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _safe_fsync_file(self, file_obj, path, operation):
        # type: (Any, Path, str) -> None
        try:
            file_obj.flush()
            os.fsync(file_obj.fileno())
        except OSError:
            if os.name != "posix":
                # log
                logger.debug(
                    "Jobs Store fsync skipped on non-posix os : operation=%s path=%s",
                    operation,
                    path,
                    exc_info=True,
                )
                #
                return

            # log
            logger.exception(
                "Jobs Store fsync failed : operation=%s path=%s",
                operation,
                path,
            )
            #
            raise

    def _read_json_object(self, path):
        # type: (Path) -> Dict[str, Any]
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except Exception:
            # log
            logger.exception(
                "Jobs Store read failed : path=%s",
                path,
            )
            #
            raise

        if not raw:
            # log
            logger.warning(
                "Jobs Store empty json file : path=%s",
                path,
            )
            #
            raise ValueError("empty json file: {}".format(path))

        try:
            data = json.loads(raw)
        except Exception:
            # log
            logger.exception(
                "Jobs Store json decode failed : path=%s",
                path,
            )
            #
            raise

        if not isinstance(data, dict):
            # log
            logger.error(
                "Jobs Store invalid root : path=%s root_type=%s",
                path,
                type(data).__name__,
            )
            #
            raise ValueError("json root must be object: {}".format(path))

        return data

    def _backup_current_state(self):
        # type: () -> None
        if not self.path.exists():
            return

        try:
            self._read_json_object(self.path)
        except Exception:
            # log
            logger.warning(
                "Jobs Store backup skipped [primary state invalid] : path=%s backup_path=%s",
                self.path,
                self.backup_path,
                exc_info=True,
            )
            #
            return

        try:
            shutil.copyfile(self.path, self.backup_path)

            with open(self.backup_path, "r+b") as f:
                self._safe_fsync_file(
                    f,
                    path=self.backup_path,
                    operation="backup",
                )

            self._fsync_dir(self.backup_path.parent)
        except Exception:
            # log
            logger.exception(
                "Jobs Store backup failed : source=%s backup=%s",
                self.path,
                self.backup_path,
            )
            #
            raise

    def _atomic_write_json(
        self,
        path,
        data,
        create_backup=True,
    ):
        # type: (Path, Dict[str, Any], bool) -> None
        path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        tmp_path = self.tmp_dir / (
            "{}.{}.{}.{}.tmp".format(
                path.name,
                os.getpid(),
                threading.get_ident(),
                uuid.uuid4().hex,
            )
        )

        try:
            encoded = (
                json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                + b"\n"
            )

            with open(tmp_path, "wb") as f:
                f.write(encoded)
                self._safe_fsync_file(
                    f,
                    path=tmp_path,
                    operation="atomic_write_tmp",
                )

            if create_backup:
                self._backup_current_state()

            os.replace(tmp_path, path)
            self._fsync_dir(path.parent)
        except Exception:
            # log
            logger.exception(
                "Jobs Store write failed : path=%s tmp_path=%s",
                path,
                tmp_path,
            )
            #
            raise
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                # log
                logger.debug(
                    "Jobs Store tmp cleanup failed : tmp_path=%s",
                    tmp_path,
                    exc_info=True,
                )
                #
                #pass

    def _ensure_state_file(self):
        # type: () -> None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._atomic_write_json(
                self.path,
                self._default_state(),
                create_backup=False,
            )
            # log
            logger.info(
                "jobs_store_state_file_created path=%s",
                self.path,
            )
            #

    def _normalize_state(self, data):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        result = self._default_state()
        result.update(data)

        try:
            result["schema_version"] = int(
                result.get("schema_version") or self.SCHEMA_VERSION
            )
        except Exception:
            # log
            logger.warning(
                "Jobs Store invalid schema_version reset : path=%s",
                self.path,
                exc_info=True,
            )
            #
            result["schema_version"] = self.SCHEMA_VERSION

        result["store_id"] = str(result.get("store_id") or "treeadmin-server-store")
        result["created_at"] = str(result.get("created_at") or self._utc_now())
        result["updated_at"] = str(result.get("updated_at") or self._utc_now())

        if not isinstance(result["queue"], list):
            # log
            logger.warning(
                "Jobs Store invalid queue reset : path=%s value_type=%s",
                self.path,
                type(result["queue"]).__name__,
            )
            #
            result["queue"] = []

        if not isinstance(result["history"], list):
            # log
            logger.warning(
                "Jobs Store invalid history reset : path=%s value_type=%s",
                self.path,
                type(result["history"]).__name__,
            )
            #
            result["history"] = []

        if not isinstance(result["responses"], list):
            # log
            logger.warning(
                "Jobs Store invalid responses reset : path=%s value_type=%s",
                self.path,
                type(result["responses"]).__name__,
            )
            #
            result["responses"] = []

        stats = result.get("stats")
        if not isinstance(stats, dict):
            # log
            logger.warning(
                "Jobs Store invalid stats reset : path=%s value_type=%s",
                self.path,
                type(stats).__name__,
            )
            #
            stats = {}

        default_stats = self._default_state()["stats"]
        default_stats.update(stats)
        result["stats"] = default_stats

        try:
            result["next_job_id"] = int(result["next_job_id"])
        except Exception:
            # log
            logger.warning(
                "Jobs Store invalid next_job_id reset : path=%s",
                self.path,
                exc_info=True,
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
                exc_info=True,
            )
            #
            result["next_response_id"] = 1

        if result["next_job_id"] <= 0:
            result["next_job_id"] = 1

        if result["next_response_id"] <= 0:
            result["next_response_id"] = 1

        self.compact_state_locked(result)
        return result

    def load_state(self):
        # type: () -> Dict[str, Any]
        self._ensure_state_file()

        try:
            data = self._read_json_object(self.path)
            return self._normalize_state(data)
        except Exception:
            # log
            logger.exception(
                "Jobs Store primary state read failed : path=%s",
                self.path,
            )
            #

        if self.backup_path.exists():
            try:
                backup_data = self._read_json_object(self.backup_path)
                state = self._normalize_state(backup_data)
                self._atomic_write_json(
                    self.path,
                    state,
                    create_backup=False,
                )

                # log
                logger.warning(
                    "Jobs Store restored from backup : primary=%s backup=%s",
                    self.path,
                    self.backup_path,
                )
                #
                return state
            except Exception:
                # log
                logger.exception(
                    "Jobs Store backup restore failed : backup=%s",
                    self.backup_path,
                )
                #

        raise RuntimeError(
            "Failed to load jobs store and backup is unavailable: {}".format(self.path)
        )

    def append_journal_locked(
        self,
        op,
        payload=None,
    ):
        # type: (str, Optional[Dict[str, Any]]) -> None
        entry = {
            "created_at": self._utc_now(),
            "op": op,
            "payload": payload or {},
        }

        self.journal_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
                f.write("\n")
                self._safe_fsync_file(
                    f,
                    path=self.journal_path,
                    operation="journal_append",
                )
        except Exception:
            # log
            logger.exception(
                "Jobs Store journal append failed : path=%s op=%s",
                self.journal_path,
                op,
            )
            #
            raise

    def save_state(
        self,
        state,
        journal_op=None,
        journal_payload=None,
    ):
        # type: (Dict[str, Any], Optional[str], Optional[Dict[str, Any]]) -> None
        normalized = self._normalize_state(state)
        normalized["updated_at"] = self._utc_now()

        if journal_op:
            self.append_journal_locked(journal_op, journal_payload)

        self._atomic_write_json(self.path, normalized)

    def compact_state_locked(self, state):
        # type: (Dict[str, Any]) -> bool
        changed = False

        history = state.get("history", [])
        if (
            isinstance(history, list)
            and self.max_history_items > 0
            and len(history) > self.max_history_items
        ):
            before_history_count = len(history) # for log
            state["history"] = history[-self.max_history_items :]
            changed = True

            # log
            logger.info(
                "Jobs Store history compacted : before_count=%s after_count=%s removed_count=%s",
                before_history_count,
                len(state["history"]),
                before_history_count - len(state["history"]),
            )
            #

        responses = state.get("responses", [])
        if (
            isinstance(responses, list)
            and self.max_responses_items > 0
            and len(responses) > self.max_responses_items
        ):
            before_responses_count = len(responses) # for log
            state["responses"] = responses[-self.max_responses_items :]
            changed = True

            # log
            logger.warning(
                "Jobs Store responses compacted : before_count=%s after_count=%s removed_count=%s",
                before_responses_count,
                len(state["responses"]),
                before_responses_count - len(state["responses"]),
            )
            #

        return changed

    def purge_expired_responses_locked(self, state):
        # type: (Dict[str, Any]) -> bool
        now = time.time()
        responses = state.get("responses", [])

        if not isinstance(responses, list):
            # log
            logger.warning(
                "Jobs Store invalid responses reset : path=%s value_type=%s",
                self.path,
                type(responses).__name__,
            )
            #
            state["responses"] = []
            return True

        before = len(responses)
        state["responses"] = [
            item
            for item in responses
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
                before - after,
            )
        #

        return changed

    def truncate_output(self, value):
        # type: (Any) -> Tuple[str, int, bool]
        text = "" if value is None else str(value)
        raw = text.encode("utf-8", errors="replace")
        original_size = len(raw)

        limit = self.max_output_bytes
        if limit <= 0 or original_size <= limit:
            return text, original_size, False

        marker = (
            "\n\n[TreeAdmin: output truncated; "
            "original_size={} bytes; limit={} bytes]\n\n".format(
                original_size,
                limit,
            )
        ).encode("utf-8")

        if len(marker) >= limit:
            truncated = marker[:limit].decode("utf-8", errors="replace")
            return truncated, original_size, True

        available = limit - len(marker)
        head_size = max(0, available // 2)
        tail_size = max(0, available - head_size)

        truncated_raw = raw[:head_size] + marker + (raw[-tail_size:] if tail_size else b"")
        truncated = truncated_raw.decode("utf-8", errors="replace")

        # log
        logger.warning(
            "Jobs Store output truncated : original_size=%s limit=%s result_size=%s",
            original_size,
            limit,
            len(truncated.encode("utf-8", errors="replace")),
        )
        #

        return truncated, original_size, True