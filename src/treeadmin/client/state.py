import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


DATA_DIR = Path("data")
DASHBOARD_STATE_PATH = DATA_DIR / "client_dashboard_state.json"

_CWD_LOCK = threading.RLock()
_PULL_LOCKS_LOCK = threading.RLock()
_SEEN_RESPONSES_LOCK = threading.RLock()
_ACTIVE_SESSIONS_LOCK = threading.RLock()
_DASHBOARD_STATE_LOCK = threading.RLock()

_SESSION_LAST_CWD = {}  # type: Dict[Tuple[str, str], str]
_PULL_LOCKS = {}  # type: Dict[Tuple[str, str], Any]
_SEEN_RESPONSE_IDS = set()  # type: Set[Tuple[str, str]]
_ACTIVE_SESSIONS = set()  # type: Set[Tuple[str, str]]


def _utc_now():
    # type: () -> str
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def set_session_cwd(target_id, session_id, cwd):
    # type: (str, str, str) -> None
    if not cwd:
        return

    with _CWD_LOCK:
        _SESSION_LAST_CWD[(target_id, session_id)] = cwd


def get_session_cwd(target_id, session_id, fallback=""):
    # type: (str, str, str) -> str
    with _CWD_LOCK:
        return _SESSION_LAST_CWD.get((target_id, session_id), fallback)


def get_pull_lock(target_id, session_id):
    # type: (str, Any) -> Any
    key = (target_id, session_id or "")

    with _PULL_LOCKS_LOCK:
        lock = _PULL_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PULL_LOCKS[key] = lock
        return lock


def response_seen_key(target_id, response_id):
    # type: (str, str) -> Tuple[str, str]
    return target_id, response_id


def is_response_seen(target_id, response_id):
    # type: (str, str) -> bool
    if not response_id:
        return False

    with _SEEN_RESPONSES_LOCK:
        return response_seen_key(target_id, response_id) in _SEEN_RESPONSE_IDS


def mark_response_seen(target_id, response_id):
    # type: (str, str) -> None
    if not response_id:
        return

    with _SEEN_RESPONSES_LOCK:
        _SEEN_RESPONSE_IDS.add(response_seen_key(target_id, response_id))


def mark_session_active(target_id, session_id):
    # type: (str, str) -> None
    if not target_id or not session_id:
        return

    with _ACTIVE_SESSIONS_LOCK:
        _ACTIVE_SESSIONS.add((target_id, session_id))

    _update_known_session_mode(target_id, session_id, "active")


def mark_session_background(target_id, session_id):
    # type: (str, str) -> None
    if not target_id or not session_id:
        return

    with _ACTIVE_SESSIONS_LOCK:
        _ACTIVE_SESSIONS.discard((target_id, session_id))

    _update_known_session_mode(target_id, session_id, "background")


def is_session_active(target_id, session_id):
    # type: (str, str) -> bool
    with _ACTIVE_SESSIONS_LOCK:
        return (target_id, session_id) in _ACTIVE_SESSIONS


def forget_session_state(target_id, session_id):
    # type: (str, str) -> None
    with _CWD_LOCK:
        _SESSION_LAST_CWD.pop((target_id, session_id), None)

    with _PULL_LOCKS_LOCK:
        _PULL_LOCKS.pop((target_id, session_id), None)

    with _ACTIVE_SESSIONS_LOCK:
        _ACTIVE_SESSIONS.discard((target_id, session_id))

    remove_known_session(target_id, session_id)


def _default_dashboard_state():
    # type: () -> Dict[str, Any]
    return {
        "ignored_responses": {},
        "pending_jobs": [],
        "known_sessions": [],
        "command_target_ids": [],
        "last_dashboard_scan_at": None,
    }


def _normalize_dashboard_state(data):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    result = _default_dashboard_state()
    result.update(data)

    if not isinstance(result.get("ignored_responses"), dict):
        result["ignored_responses"] = {}

    if not isinstance(result.get("pending_jobs"), list):
        result["pending_jobs"] = []

    if not isinstance(result.get("known_sessions"), list):
        result["known_sessions"] = []

    if not isinstance(result.get("command_target_ids"), list):
        result["command_target_ids"] = []

    return result


def load_dashboard_state():
    # type: () -> Dict[str, Any]
    with _DASHBOARD_STATE_LOCK:
        if not DASHBOARD_STATE_PATH.exists():
            return _default_dashboard_state()

        raw = DASHBOARD_STATE_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            return _default_dashboard_state()

        try:
            data = json.loads(raw)
        except ValueError:
            return _default_dashboard_state()

        if not isinstance(data, dict):
            return _default_dashboard_state()

        return _normalize_dashboard_state(data)


def save_dashboard_state(data):
    # type: (Dict[str, Any]) -> None
    with _DASHBOARD_STATE_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DASHBOARD_STATE_PATH.write_text(
            json.dumps(_normalize_dashboard_state(data), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def set_last_dashboard_scan_now():
    # type: () -> None
    data = load_dashboard_state()
    data["last_dashboard_scan_at"] = _utc_now()
    save_dashboard_state(data)


def is_response_ignored(target_id, response_id):
    # type: (str, str) -> bool
    if not target_id or not response_id:
        return False

    data = load_dashboard_state()
    ignored = data.get("ignored_responses", {})
    ids = ignored.get(target_id, [])

    if not isinstance(ids, list):
        return False

    return str(response_id) in {str(item) for item in ids}


def ignore_response(target_id, response_id):
    # type: (str, str) -> None
    if not target_id or not response_id:
        return

    data = load_dashboard_state()
    ignored = data.setdefault("ignored_responses", {})
    ids = ignored.setdefault(target_id, [])

    if not isinstance(ids, list):
        ids = []
        ignored[target_id] = ids

    response_id = str(response_id)
    if response_id not in ids:
        ids.append(response_id)

    save_dashboard_state(data)


def ignore_responses(target_id, response_ids):
    # type: (str, List[str]) -> None
    for response_id in response_ids:
        ignore_response(target_id, response_id)


def remove_ignored_response(target_id, response_id):
    # type: (str, str) -> None
    data = load_dashboard_state()
    ignored = data.get("ignored_responses", {})
    ids = ignored.get(target_id, [])

    if isinstance(ids, list):
        ignored[target_id] = [item for item in ids if str(item) != str(response_id)]

    save_dashboard_state(data)


def register_command_target(target_id):
    # type: (str) -> None
    target_id = str(target_id).strip()
    if not target_id:
        return

    data = load_dashboard_state()
    targets = data.setdefault("command_target_ids", [])

    if not isinstance(targets, list):
        targets = []
        data["command_target_ids"] = targets

    if target_id not in {str(item) for item in targets}:
        targets.append(target_id)

    save_dashboard_state(data)


def register_pending_job(
    target_id,
    session_id,
    job_id,
    command,
):
    target_id = str(target_id).strip()
    session_id = str(session_id).strip()
    job_id = str(job_id).strip()

    if not target_id or not session_id or not job_id:
        return

    data = load_dashboard_state()

    targets = data.setdefault("command_target_ids", [])
    if isinstance(targets, list) and target_id not in {str(item) for item in targets}:
        targets.append(target_id)

    pending = data.setdefault("pending_jobs", [])

    item = {
        "target_id": target_id,
        "session_id": session_id,
        "job_id": job_id,
        "command": str(command),
        "created_at": _utc_now(),
    }

    if isinstance(pending, list):
        already_exists = False
        for old in pending:
            if not isinstance(old, dict):
                continue

            if (
                str(old.get("target_id", "")) == target_id
                and str(old.get("job_id", "")) == job_id
            ):
                already_exists = True
                break

        if not already_exists:
            pending.append(item)

    save_dashboard_state(data)


def mark_pending_job_done(target_id, job_id):
    # type: (str, str) -> None
    data = load_dashboard_state()
    pending = data.get("pending_jobs", [])

    if isinstance(pending, list):
        data["pending_jobs"] = [
            item
            for item in pending
            if not (
                isinstance(item, dict)
                and str(item.get("target_id", "")) == str(target_id)
                and str(item.get("job_id", "")) == str(job_id)
            )
        ]

    save_dashboard_state(data)


def get_pending_job_target_ids():
    # type: () -> Set[str]
    data = load_dashboard_state()
    pending = data.get("pending_jobs", [])

    result = set()  # type: Set[str]
    if isinstance(pending, list):
        for item in pending:
            if isinstance(item, dict):
                target_id = str(item.get("target_id", "")).strip()
                if target_id:
                    result.add(target_id)

    return result


def register_known_session(
    target_id,
    session_id,
    cwd="",
    mode="background",
):
    target_id = str(target_id).strip()
    session_id = str(session_id).strip()

    if not target_id or not session_id:
        return

    if mode not in {"active", "background"}:
        mode = "background"

    data = load_dashboard_state()

    targets = data.setdefault("command_target_ids", [])
    if isinstance(targets, list) and target_id not in {str(item) for item in targets}:
        targets.append(target_id)

    sessions = data.setdefault("known_sessions", [])
    if not isinstance(sessions, list):
        sessions = []
        data["known_sessions"] = sessions

    updated = False

    for item in sessions:
        if not isinstance(item, dict):
            continue

        if (
            str(item.get("target_id", "")) == target_id
            and str(item.get("session_id", "")) == session_id
        ):
            item["cwd"] = str(cwd or item.get("cwd", ""))
            item["mode"] = mode
            item["updated_at"] = _utc_now()
            updated = True
            break

    if not updated:
        sessions.append(
            {
                "target_id": target_id,
                "session_id": session_id,
                "cwd": str(cwd or ""),
                "mode": mode,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
            }
        )

    save_dashboard_state(data)


def _update_known_session_mode(target_id, session_id, mode):
    # type: (str, str, str) -> None
    target_id = str(target_id).strip()
    session_id = str(session_id).strip()

    if not target_id or not session_id:
        return

    data = load_dashboard_state()
    sessions = data.get("known_sessions", [])

    if not isinstance(sessions, list):
        return

    changed = False

    for item in sessions:
        if not isinstance(item, dict):
            continue

        if (
            str(item.get("target_id", "")) == target_id
            and str(item.get("session_id", "")) == session_id
        ):
            item["mode"] = mode
            item["updated_at"] = _utc_now()
            changed = True
            break

    if changed:
        save_dashboard_state(data)


def remove_known_session(target_id, session_id):
    # type: (str, str) -> None
    data = load_dashboard_state()
    sessions = data.get("known_sessions", [])

    if isinstance(sessions, list):
        data["known_sessions"] = [
            item
            for item in sessions
            if not (
                isinstance(item, dict)
                and str(item.get("target_id", "")) == str(target_id)
                and str(item.get("session_id", "")) == str(session_id)
            )
        ]

    save_dashboard_state(data)


def get_known_sessions():
    # type: () -> List[Dict[str, Any]]
    data = load_dashboard_state()
    sessions = data.get("known_sessions", [])

    result = []  # type: List[Dict[str, Any]]

    if isinstance(sessions, list):
        for item in sessions:
            if not isinstance(item, dict):
                continue

            target_id = str(item.get("target_id", "")).strip()
            session_id = str(item.get("session_id", "")).strip()

            if not target_id or not session_id:
                continue

            result.append(dict(item))

    return result


def get_known_session_target_ids():
    # type: () -> Set[str]
    result = set()  # type: Set[str]

    for item in get_known_sessions():
        target_id = str(item.get("target_id", "")).strip()
        if target_id:
            result.add(target_id)

    return result


def get_command_target_ids():
    # type: () -> Set[str]
    data = load_dashboard_state()
    raw_targets = data.get("command_target_ids", [])

    result = set()  # type: Set[str]

    if isinstance(raw_targets, list):
        for item in raw_targets:
            target_id = str(item).strip()
            if target_id:
                result.add(target_id)

    return result


def get_notification_target_ids():
    # type: () -> Set[str]
    result = set()  # type: Set[str]

    result.update(get_command_target_ids())
    result.update(get_pending_job_target_ids())
    result.update(get_known_session_target_ids())

    return result