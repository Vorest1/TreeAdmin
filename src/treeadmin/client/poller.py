from __future__ import annotations

import os
import threading

from src.treeadmin.client import api
from src.treeadmin.client import state
from src.treeadmin.client.results import pull_pending_results

ACTIVE_POLL_INTERVAL_SECONDS = float(os.getenv("TREEADMIN_ACTIVE_POLL_INTERVAL", "0.5"))
BACKGROUND_POLL_INTERVAL_SECONDS = float(os.getenv("TREEADMIN_BACKGROUND_POLL_INTERVAL", "7.0"))

_POLLERS_LOCK = threading.RLock()
_RESULT_POLLERS: dict[tuple[str, str], threading.Thread] = {}
_RESULT_POLL_STOP_EVENTS: dict[tuple[str, str], threading.Event] = {}
_RESULT_POLL_MODES: dict[tuple[str, str], str] = {}


def _poll_interval_for_mode(mode: str) -> float:
    if mode == "active":
        return ACTIVE_POLL_INTERVAL_SECONDS

    return BACKGROUND_POLL_INTERVAL_SECONDS


def _poll_active_session(target_id: str, session_id: str) -> None:
    pull_pending_results(target_id, session_id, quiet=True)


def _poll_background_session(target_id: str, session_id: str) -> None:
    api.get_results_summary(target_id, session_id=session_id)


def _result_poller_loop(
    target_id: str,
    session_id: str,
    stop_event: threading.Event,
) -> None:
    key = (target_id, session_id)

    while not stop_event.is_set():
        with _POLLERS_LOCK:
            mode = _RESULT_POLL_MODES.get(key, "background")

        try:
            if mode == "active":
                _poll_active_session(target_id, session_id)
            else:
                _poll_background_session(target_id, session_id)
        except Exception:
            pass

        stop_event.wait(_poll_interval_for_mode(mode))


def start_or_update_result_poller(target_id: str, session_id: str, mode: str) -> None:
    if mode not in {"active", "background"}:
        raise ValueError("poller mode must be 'active' or 'background'")

    key = (target_id, session_id)

    with _POLLERS_LOCK:
        _RESULT_POLL_MODES[key] = mode

        thread = _RESULT_POLLERS.get(key)
        if thread is not None and thread.is_alive():
            return

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_result_poller_loop,
            args=(target_id, session_id, stop_event),
            daemon=True,
            name=f"result-poller-{target_id}-{session_id}",
        )

        _RESULT_POLL_STOP_EVENTS[key] = stop_event
        _RESULT_POLLERS[key] = thread
        thread.start()


def restore_background_pollers_from_state() -> None:
    for item in state.get_known_sessions():
        target_id = str(item.get("target_id", "")).strip()
        session_id = str(item.get("session_id", "")).strip()
        if not target_id or not session_id:
            continue

        state.mark_session_background(target_id, session_id)
        start_or_update_result_poller(target_id, session_id, "background")


def stop_result_poller(target_id: str, session_id: str) -> None:
    key = (target_id, session_id)

    with _POLLERS_LOCK:
        stop_event = _RESULT_POLL_STOP_EVENTS.pop(key, None)
        _RESULT_POLLERS.pop(key, None)
        _RESULT_POLL_MODES.pop(key, None)

    if stop_event is not None:
        stop_event.set()


def stop_all_result_pollers() -> None:
    with _POLLERS_LOCK:
        items = list(_RESULT_POLL_STOP_EVENTS.items())
        _RESULT_POLL_STOP_EVENTS.clear()
        _RESULT_POLLERS.clear()
        _RESULT_POLL_MODES.clear()

    for _, stop_event in items:
        stop_event.set()