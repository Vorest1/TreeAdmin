import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.treeadmin.client import api
from src.treeadmin.client import state
from src.treeadmin.client.poller import (
    start_or_update_result_poller,
    stop_result_poller,
)
from src.treeadmin.client.results import pull_pending_results
from src.treeadmin.client.terminal import ui_input, ui_print


logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("treeadmin.audit")


OUTPUT_FILE_RE = re.compile(r"\s+#F\[(.+?)\]F#\s*$")


AVAILABLE_PATTERNS = [  # type: List[Dict[str, str]]
    {
        "name": "Save output to file",
        "syntax": "#F[путь_к_файлу]F#",
        "example_linux": "ls -la #F[/tmp/ls_result.txt]F#",
        "regex": r"\s+#F\[(.+?)\]F#\s*$",
        "description": (
            "Выражение указывается в конце команды и задаёт файл, "
            "в который должен быть сохранён результат выполнения команды. "
            "Сейчас в queued-режиме автоматическое сохранение результата "
            "по этому выражению пока отключено: клиент только распознаёт выражение "
            "и предупреждает пользователя после постановки команды в очередь."
        ),
    },
]


def _extract_output_file(raw_command):
    # type: (str) -> Tuple[str, Optional[str]]
    match = OUTPUT_FILE_RE.search(raw_command)
    if not match:
        return raw_command.strip(), None

    output_path = match.group(1).strip()
    cleaned_command = raw_command[:match.start()].strip()
    return cleaned_command, output_path


def _save_command_output(output_path, text):
    # type: (str, str) -> None
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _print_session_jobs(data):
    # type: (Dict[str, Any]) -> None
    lines = ["\nSession {} queue:".format(data.get("session_id"))]  # type: List[str]

    queue_items = data.get("queue", [])
    if not queue_items:
        lines.append("  <empty>")
    else:
        for item in queue_items:
            lines.append(
                "  [{}] {} :: {}".format(
                    item.get("job_id"),
                    item.get("status"),
                    item.get("command")
                )
            )

    lines.append("History:")
    history_items = data.get("history", [])
    if not history_items:
        lines.append("  <empty>")
    else:
        for item in history_items[-10:]:
            lines.append(
                "  [{}] {} rc={} :: {}".format(
                    item.get("job_id"),
                    item.get("status"),
                    item.get("returncode"),
                    item.get("command")
                )
            )

    lines.append("Pending responses:")
    response_items = data.get("responses", [])
    if not response_items:
        lines.append("  <empty>")
    else:
        for item in response_items:
            lines.append(
                "  [response {}] job={} :: {}".format(
                    item.get("response_id"),
                    item.get("job_id"),
                    item.get("command")
                )
            )

    ui_print("\n".join(lines))


def _print_sessions(sessions):
    # type: (List[Dict[str, Any]]) -> None
    if not sessions:
        ui_print("No active sessions on server")
        return

    lines = ["\nActive sessions:"]
    for item in sessions:
        session_id = str(item.get("session_id", ""))
        platform_name = str(item.get("platform", ""))
        cwd = str(item.get("cwd", ""))
        lines.append(
            "  session_id={} platform={} cwd={}".format(
                session_id,
                platform_name,
                cwd
            )
        )

    ui_print("\n".join(lines))


def _print_available_patterns():
    # type: () -> None
    lines = [  # type: List[str]
        "\nAvailable command patterns:",
        "",
    ]

    for index, item in enumerate(AVAILABLE_PATTERNS, start=1):
        lines.append("{}) {}".format(index, item["name"]))
        lines.append("   Syntax:          {}".format(item["syntax"]))
        lines.append("   Linux example:   {}".format(item["example_linux"]))
        lines.append("   Regex:           {}".format(item["regex"]))
        lines.append("   Description:     {}".format(item["description"]))
        lines.append("")

    ui_print("\n".join(lines).rstrip())


def _print_help():
    # type: () -> None
    ui_print(
        "\nAvailable commands:\n"
        "  help       show this help\n"
        "  jobs       show queued and completed commands in this session\n"
        "  sessions   show active sessions on current server\n"
        "  pull       fetch undelivered results from server now\n"
        "  patterns   show available command patterns\n"
        "  exit       leave this shell window, keep remote session open\n"
        "  close      close remote session and leave\n"
        "\nCommand patterns:\n"
        "  Use 'patterns' to show all supported command patterns.\n"
        "  Current pattern example: ls -la #F[/tmp/ls_result.txt]F#"
    )


def _open_shell(target_id):
    # type: (str) -> Tuple[Optional[str], Optional[str]]
    # log
    logger.info(
        "Client _shell_open requested : target_id=%s",
        target_id
    )
    #

    status, result = api.open_shell_request(target_id)

    if status != 200:
        # log
        res = str(result).strip()
        if len(res) > 160:
            res = res[: 157] + "..."
        logger.warning(
            "Client _shell_open failed : target_id=%s status=%s result=%s",
            target_id,
            status,
            res
        )
        #
        ui_print("[{}] {}".format(status, result))
        return None, None

    if not isinstance(result, dict):
        # log
        logger.warning(
            "Client _open_shell invalid response : target_id=%s response_type=%s",
            target_id,
            type(result).__name__
        )
        #
        ui_print("[502] invalid shell response: {}".format(result))
        return None, None

    session_id = result.get("session_id")
    cwd = result.get("cwd", "")

    if not isinstance(session_id, str) or not session_id.strip():
        logger.warning(
            "Client _open_shell invalid session_id : target_id=%s session_id=%s",
            target_id,
            session_id
        )
        ui_print("[502] server returned invalid session_id")
        return None, None

    if not isinstance(cwd, str):
        cwd = ""

    state.set_session_cwd(target_id, session_id, cwd)
    state.register_known_session(target_id, session_id, cwd=cwd, mode="active")
    state.mark_session_active(target_id, session_id)
    start_or_update_result_poller(target_id, session_id, "active")
    pull_pending_results(target_id, session_id, quiet=True)

    # log
    logger.info(
        "Client shell opened : target_id=%s session_id=%s cwd=%s",
        target_id,
        session_id,
        cwd
    )

    audit_logger.info(
        "Client shell opened : target_id=%s session_id=%s cwd=%s",
        target_id,
        session_id,
        cwd
    )
    #

    return session_id, cwd


def _attach_shell(target_id, session_id):
    # type: (str, str) -> bool
    # log
    logger.info(
        "Client _attach_shell requested : target_id=%s session_id=%s",
        target_id,
        session_id
    )
    #

    sessions = api.list_sessions(target_id)
    if sessions is None:
        # log
        logger.warning(
            "Client _attach_shell failed list sessions : target_id=%s session_id=%s",
            target_id,
            session_id
        )
        #
        ui_print("Failed to list sessions")
        return False

    matched_session = None  # type: Optional[Dict[str, Any]]

    for item in sessions:
        if str(item.get("session_id", "")) == session_id:
            matched_session = item
            break

    if matched_session is None:
        # log
        logger.warning(
            "Client _attach_shell session not found : target_id=%s session_id=%s",
            target_id,
            session_id
        )
        #
        ui_print("Session not found on server: {}".format(session_id))
        return False

    cwd = str(matched_session.get("cwd", ""))
    state.register_known_session(target_id, session_id, cwd=cwd, mode="active")

    state.mark_session_active(target_id, session_id)
    start_or_update_result_poller(target_id, session_id, "active")
    pull_pending_results(target_id, session_id, quiet=True)

    # log
    logger.info(
        "Client shell attached : target_id=%s session_id=%s cwd=%s",
        target_id,
        session_id,
        cwd
    )

    audit_logger.info(
        "Client shell attached : target_id=%s session_id=%s cwd=%s",
        target_id,
        session_id,
        cwd
    )
    #

    return True


def interactive_shell(
    target_id,
    existing_session_id=None,
    existing_cwd=None,
):
    # type: (str, Optional[str], Optional[str]) -> None
    if existing_session_id is None:
        session_id, current_dir = _open_shell(target_id)
    else:
        if not _attach_shell(target_id, existing_session_id):
            return

        session_id = existing_session_id
        current_dir = existing_cwd or ""
        state.set_session_cwd(target_id, session_id, current_dir)

    if not session_id:
        # log
        logger.warning(
            "Client shell start failed : target_id=%s existing_session_id=%s",
            target_id,
            existing_session_id
        )
        #
        ui_print("Failed to open shell session")
        return

    current_dir = current_dir or ""
    close_remote_on_exit = False

    state.mark_session_active(target_id, session_id)
    start_or_update_result_poller(target_id, session_id, "active")

    # log
    logger.info(
        "Client shell loop started : target_id=%s session_id=%s cwd=%s",
        target_id,
        session_id,
        current_dir
    )
    #

    ui_print("Commands are queued automatically. Results are fetched from server in background.")
    ui_print("Type 'help' to show available commands.")

    while True:
        try:
            prompt_dir = state.get_session_cwd(target_id, session_id, current_dir)
            raw_cmd = ui_input(
                "[{}][session {}] {} > ".format(
                    target_id,
                    session_id,
                    prompt_dir
                )
            ).strip()
        except KeyboardInterrupt:
            state.mark_session_background(target_id, session_id)
            start_or_update_result_poller(target_id, session_id, "background")

            # log
            logger.info(
                "Client shell left background by keyboard_interrupt : target_id=%s session_id=%s",
                target_id,
                session_id
            )

            audit_logger.info(
                "Client shell left background : target_id=%s session_id=%s reason=keyboard_interrupt",
                target_id,
                session_id
            )
            #

            ui_print(
                "Left session {}. Remote session is still active and will be polled in background.".format(
                    session_id
                )
            )
            return

        if not raw_cmd:
            continue

        lowered = raw_cmd.lower()

        if lowered in {"help", ":help"}:
            _print_help()
            continue

        if lowered in {"patterns", ":patterns"}:
            _print_available_patterns()
            continue

        if lowered in {"jobs", ":jobs"}:
            data = api.get_session_jobs(target_id, session_id)
            if data is None:
                # log
                logger.warning(
                    "Client shell jobs request failed : target_id=%s session_id=%s",
                    target_id,
                    session_id
                )
                #
                ui_print("Failed to get session jobs")
            else:
                _print_session_jobs(data)
            continue

        if lowered in {"sessions", ":sessions"}:
            sessions = api.list_sessions(target_id)
            if sessions is None:
                # log
                logger.warning(
                    "Client shell sessions request failed : target_id=%s session_id=%s",
                    target_id,
                    session_id
                )
                #
                ui_print("Failed to list sessions")
            else:
                _print_sessions(sessions)
            continue

        if lowered in {"pull", ":pull"}:
            count = pull_pending_results(target_id, session_id, quiet=False)
            # log
            logger.info(
                "Client shell manual pull : target_id=%s session_id=%s pulled_count=%s",
                target_id,
                session_id,
                count
            )
            #
            ui_print("Pulled responses: {}".format(count))
            continue

        if lowered in {"exit", "quit", ":leave", "leave"}:
            state.mark_session_background(target_id, session_id)
            start_or_update_result_poller(target_id, session_id, "background")
            # log
            logger.info(
                "Client shell left background : target_id=%s session_id=%s",
                target_id,
                session_id,
            )

            audit_logger.info(
                "Client shell left background : target_id=%s session_id=%s reason=user_exit",
                target_id,
                session_id,
            )
            #

            ui_print(
                "Left session {}. Remote session is still active and will be polled in background.".format(
                    session_id
                )
            )
            return

        if lowered in {"close", ":close"}:
            # log
            logger.info(
                "Client shell close requested : target_id=%s session_id=%s",
                target_id,
                session_id
            )
            #
            close_remote_on_exit = True
            break

        cmd, output_file = _extract_output_file(raw_cmd)
        if not cmd:
            ui_print("Empty command")
            continue

        status, result = api.send_queued_command(target_id, session_id, cmd)

        if status not in {200, 202}:
            
            # log
            short_err = str(result.get("error", "") if isinstance(result, dict) else result).strip()
            if len(short_err) > 160:
                short_err = short_err[: 157] + "..."
            logger.warning(
                "Client command queue failed : target_id=%s session_id=%s status=%s "
                "command_len=%s error=%s",
                target_id,
                session_id,
                status,
                len(cmd),
                short_err
            )
            #
            ui_print("[{}] {}".format(status, result.get("error", "")))
            continue

        job_id = str(result.get("job_id", "")).strip()
        ui_print("Queued job {} on session {}: {}".format(job_id, session_id, cmd))

        # log
        logger.info(
            "Client command queued : target_id=%s session_id=%s job_id=%s command_len=%s "
            "output_file_requested=%s",
            target_id,
            session_id,
            job_id,
            len(cmd),
            bool(output_file)
        )

        short_cmd = str(cmd).strip()
        if len(short_cmd) > 160:
            short_cmd = short_cmd[: 157] + "..."

        audit_logger.info(
            "Client command queued : target_id=%s session_id=%s job_id=%s command_preview=%s",
            target_id,
            session_id,
            job_id,
            short_cmd
        )
        #

        if job_id:
            state.register_pending_job(
                target_id=target_id,
                session_id=session_id,
                job_id=job_id,
                command=cmd,
            )

        if output_file:
            # log
            logger.info(
                "Client command output file pattern detected : target_id=%s session_id=%s "
                "job_id=%s output_path=%s",
                target_id,
                session_id,
                job_id,
                output_file
            )
            #
            ui_print(
                "#F[...]F# is not applied automatically in queued mode. "
                "Save the result manually after it arrives."
            )

    if close_remote_on_exit:
        status, response = api.close_shell(target_id, session_id)
        stop_result_poller(target_id, session_id)
        state.forget_session_state(target_id, session_id)

        if status != 200:
            # log
            short_resp = str(response).strip()
            if len(response) > 160:
                short_resp = short_resp[: 157] + "..."
            logger.warning(
                "Client shell close failed : target_id=%s session_id=%s status=%s response=%s",
                target_id,
                session_id,
                status,
                short_resp
            )
            #
            ui_print("[{}] {}".format(status, response))
        else:
            # log
            logger.info(
                "Client shell closed : target_id=%s session_id=%s",
                target_id,
                session_id
            )

            audit_logger.info(
                "Client shell closed : target_id=%s session_id=%s",
                target_id,
                session_id
            )
            #
            ui_print("Closed remote session: {}".format(session_id))