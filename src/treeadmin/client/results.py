from __future__ import annotations

from typing import Any

from src.treeadmin.client import api
from src.treeadmin.client import state
from src.treeadmin.client.terminal import ui_print

import logging

logger = logging.getLogger(__name__)

def _output_size(value: Any) -> int:
    if value is None:
        return 0

    return len(str(value).encode("utf-8", errors="replace"))


def _short_text(value: Any, limit: int = 180) -> str:
    text = str(value).strip()

    if len(text) <= limit:
        return text

    return text[: limit - 3] + "..."

def format_response_payload(payload: dict[str, Any]) -> str:
    node_id = str(payload.get("node_id", "unknown-node"))
    session_id = str(payload.get("session_id", ""))
    job_id = str(payload.get("job_id", ""))
    command = str(payload.get("command", ""))
    status = str(payload.get("status", ""))
    output = str(payload.get("output", ""))
    cwd = str(payload.get("cwd", ""))
    returncode = payload.get("returncode")
    error = payload.get("error")

    lines: list[str] = [
        "",
        f"[from {node_id}][session {session_id}][job {job_id}] {command}",
    ]

    if status:
        lines.append(f"[status={status}]")

    if output:
        lines.append(output)

    if cwd:
        lines.append(f"[cwd={cwd}]")

    if isinstance(returncode, int):
        lines.append(f"[returncode={returncode}]")

    if error:
        lines.append(f"[error={error}]")

    return "\n".join(lines)


def print_response_payload(payload: dict[str, Any]) -> None:
    ui_print(format_response_payload(payload))


def pull_pending_results(
    target_id: str,
    session_id: str | None = None,
    *,
    quiet: bool = False,
) -> int:

    pull_lock = state.get_pull_lock(target_id, session_id)

    acquired = pull_lock.acquire(blocking=False)
    if not acquired:
        # log
        logger.debug(
            "pull_pending_results skipped [lock busy] : target_id=%s session_id=%s",
            target_id,
            session_id
        )
        #
        return 0

    try:
        status, result = api.pull_pending_results_raw(target_id, session_id)

        if status != 200:
            # log
            log_f = logger.debug if quiet else logger.warning
            log_f(
                "pull_pending_results failed : target_id=%s session_id=%s status=%s result=%s",
                target_id,
                session_id,
                status,
                _short_text(result)
            )
            #
            if not quiet:
                ui_print(f"[{status}] failed to pull pending results: {result}")
            return 0

        if not isinstance(result, list):
            # log
            logger.warning(
                "pull_pending_results invalid result : target_id=%s session_id=%s "
                "result_type=%s",
                target_id,
                session_id,
                type(result).__name__
            )
            #
            if not quiet:
                ui_print(f"[502] invalid pull result: {result}")
            return 0

        response_ids_to_ack: list[str] = []
        printed_count = 0

        for item in result:
            if not isinstance(item, dict):
                # log
                logger.warning(
                    "pull_pending_results invalid item : target_id=%s session_id=%s "
                    "item_type=%s",
                    target_id,
                    session_id,
                    type(item).__name__
                )
                #
                continue

            response_id = str(item.get("response_id", "")).strip()
            if not response_id:
                # log
                logger.warning(
                    "pull_pending_results item without response_id : target_id=%s session_id=%s",
                    target_id,
                    session_id,
                )
                #
                continue

            response_ids_to_ack.append(response_id)

            if state.is_response_seen(target_id, response_id):
                continue

            state.mark_response_seen(target_id, response_id)
            print_response_payload(item)
            printed_count += 1

            response_session_id = str(item.get("session_id", "")).strip()
            response_cwd = str(item.get("cwd", "")).strip()
            response_job_id = str(item.get("job_id", "")).strip()
            response_status = str(item.get("status", ""))
            returncode = item.get("returncode")
            
            # log
            logger.info(
                "response printed : target_id=%s response_id=%s job_id=%s "
                "session_id=%s status=%s returncode=%s output_size=%s",
                target_id,
                response_id,
                response_job_id,
                response_session_id,
                response_status,
                returncode,
                _output_size(item.get("output", ""))
            )
            #
            
            if response_session_id and response_cwd:
                state.set_session_cwd(target_id, response_session_id, response_cwd)

            if response_job_id:
                state.mark_pending_job_done(target_id, response_job_id)

        if response_ids_to_ack:
            ack_status, ack_raw = api.ack_responses(target_id, response_ids_to_ack)
            if ack_status != 200:
                # log
                logger.warning(
                    "pull_pending_results ack failed : target_id=%s session_id=%s "
                    "status=%s response_count=%s result=%s",
                    target_id,
                    session_id,
                    ack_status,
                    len(response_ids_to_ack),
                    _short_text(ack_raw)
                )
                #
                if not quiet:
                    ui_print(f"[{ack_status}] failed to ack pulled results: {ack_raw}")
            else:
                # log
                logger.debug(
                    "pull_pending_results ack done : target_id=%s session_id=%s "
                    "response_count=%s printed_count=%s",
                    target_id,
                    session_id,
                    len(response_ids_to_ack),
                    printed_count
                )
                #

        return printed_count
    
    except Exception:
        logger.exception(
            "pull_pending_results unexpected error : target_id=%s session_id=%s quiet=%s",
            target_id,
            session_id,
            quiet
        )
        raise

    finally:
        pull_lock.release()