from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.treeadmin.client import api
from src.treeadmin.client import state
from src.treeadmin.client.poller import (
    start_or_update_result_poller,
    stop_result_poller,
)
from src.treeadmin.client.results import pull_pending_results
from src.treeadmin.client.terminal import ui_input, ui_print


OUTPUT_FILE_RE = re.compile(r"\s+#F\[(.+?)\]F#\s*$")


AVAILABLE_PATTERNS: list[dict[str, str]] = [
    {
        "name": "Save output to file",
        "syntax": "#F[путь_к_файлу]F#",
        "example_windows": r"dir #F[C:\Temp\dir_result.txt]F#",
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


def _extract_output_file(raw_command: str) -> tuple[str, str | None]:
    match = OUTPUT_FILE_RE.search(raw_command)
    if not match:
        return raw_command.strip(), None

    output_path = match.group(1).strip()
    cleaned_command = raw_command[:match.start()].strip()
    return cleaned_command, output_path


def _save_command_output(output_path: str, text: str) -> None:
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _print_session_jobs(data: dict[str, Any]) -> None:
    lines: list[str] = [f"\nSession {data.get('session_id')} queue:"]

    queue_items = data.get("queue", [])
    if not queue_items:
        lines.append("  <empty>")
    else:
        for item in queue_items:
            lines.append(
                f"  [{item.get('job_id')}] {item.get('status')} :: {item.get('command')}"
            )

    lines.append("History:")
    history_items = data.get("history", [])
    if not history_items:
        lines.append("  <empty>")
    else:
        for item in history_items[-10:]:
            lines.append(
                f"  [{item.get('job_id')}] {item.get('status')} "
                f"rc={item.get('returncode')} :: {item.get('command')}"
            )

    lines.append("Pending responses:")
    response_items = data.get("responses", [])
    if not response_items:
        lines.append("  <empty>")
    else:
        for item in response_items:
            lines.append(
                f"  [response {item.get('response_id')}] "
                f"job={item.get('job_id')} :: {item.get('command')}"
            )

    ui_print("\n".join(lines))


def _print_sessions(sessions: list[dict[str, Any]]) -> None:
    if not sessions:
        ui_print("No active sessions on server")
        return

    lines = ["\nActive sessions:"]
    for item in sessions:
        session_id = str(item.get("session_id", ""))
        platform_name = str(item.get("platform", ""))
        cwd = str(item.get("cwd", ""))
        lines.append(f"  session_id={session_id} platform={platform_name} cwd={cwd}")

    ui_print("\n".join(lines))


def _print_available_patterns() -> None:
    lines: list[str] = [
        "\nAvailable command patterns:",
        "",
    ]

    for index, item in enumerate(AVAILABLE_PATTERNS, start=1):
        lines.append(f"{index}) {item['name']}")
        lines.append(f"   Syntax:          {item['syntax']}")
        lines.append(f"   Windows example: {item['example_windows']}")
        lines.append(f"   Linux example:   {item['example_linux']}")
        lines.append(f"   Regex:           {item['regex']}")
        lines.append(f"   Description:     {item['description']}")
        lines.append("")

    ui_print("\n".join(lines).rstrip())


def _print_help() -> None:
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
        "  Current pattern example: dir #F[C:\\Temp\\dir_result.txt]F#"
    )


def _open_shell(target_id: str) -> tuple[str | None, str | None]:
    status, result = api.open_shell_request(target_id)

    if status != 200:
        ui_print(f"[{status}] {result}")
        return None, None

    if not isinstance(result, dict):
        ui_print(f"[502] invalid shell response: {result}")
        return None, None

    session_id = result.get("session_id")
    cwd = result.get("cwd", "")

    if not isinstance(session_id, str) or not session_id.strip():
        ui_print("[502] server returned invalid session_id")
        return None, None

    if not isinstance(cwd, str):
        cwd = ""

    state.set_session_cwd(target_id, session_id, cwd)
    state.register_known_session(target_id, session_id, cwd=cwd, mode="active")
    state.mark_session_active(target_id, session_id)
    start_or_update_result_poller(target_id, session_id, "active")
    pull_pending_results(target_id, session_id, quiet=True)

    return session_id, cwd


def _attach_shell(target_id: str, session_id: str) -> bool:
    sessions = api.list_sessions(target_id)
    if sessions is None:
        ui_print("Failed to list sessions")
        return False

    matched_session: dict[str, Any] | None = None

    for item in sessions:
        if str(item.get("session_id", "")) == session_id:
            matched_session = item
            break

    if matched_session is None:
        ui_print(f"Session not found on server: {session_id}")
        return False

    cwd = str(matched_session.get("cwd", ""))
    state.register_known_session(target_id, session_id, cwd=cwd, mode="active")

    state.mark_session_active(target_id, session_id)
    start_or_update_result_poller(target_id, session_id, "active")
    pull_pending_results(target_id, session_id, quiet=True)

    return True


def interactive_shell(
    target_id: str,
    existing_session_id: str | None = None,
    existing_cwd: str | None = None,
) -> None:
    if existing_session_id is None:
        session_id, current_dir = _open_shell(target_id)
    else:
        if not _attach_shell(target_id, existing_session_id):
            return

        session_id = existing_session_id
        current_dir = existing_cwd or ""
        state.set_session_cwd(target_id, session_id, current_dir)

    if not session_id:
        ui_print("Failed to open shell session")
        return

    current_dir = current_dir or ""
    close_remote_on_exit = False

    state.mark_session_active(target_id, session_id)
    start_or_update_result_poller(target_id, session_id, "active")

    ui_print("Commands are queued automatically. Results are fetched from server in background.")
    ui_print("Type 'help' to show available commands.")

    while True:
        try:
            prompt_dir = state.get_session_cwd(target_id, session_id, current_dir)
            raw_cmd = ui_input(f"[{target_id}][session {session_id}] {prompt_dir} > ").strip()
        except KeyboardInterrupt:
            state.mark_session_background(target_id, session_id)
            start_or_update_result_poller(target_id, session_id, "background")
            ui_print(f"Left session {session_id}. Remote session is still active and will be polled in background.")
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
                ui_print("Failed to get session jobs")
            else:
                _print_session_jobs(data)
            continue

        if lowered in {"sessions", ":sessions"}:
            sessions = api.list_sessions(target_id)
            if sessions is None:
                ui_print("Failed to list sessions")
            else:
                _print_sessions(sessions)
            continue

        if lowered in {"pull", ":pull"}:
            count = pull_pending_results(target_id, session_id, quiet=False)
            ui_print(f"Pulled responses: {count}")
            continue

        if lowered in {"exit", "quit", ":leave", "leave"}:
            state.mark_session_background(target_id, session_id)
            start_or_update_result_poller(target_id, session_id, "background")
            ui_print(f"Left session {session_id}. Remote session is still active and will be polled in background.")
            return

        if lowered in {"close", ":close"}:
            close_remote_on_exit = True
            break

        cmd, output_file = _extract_output_file(raw_cmd)
        if not cmd:
            ui_print("Empty command")
            continue

        status, result = api.send_queued_command(target_id, session_id, cmd)

        if status not in {200, 202}:
            ui_print(f"[{status}] {result.get('error', '')}")
            continue

        job_id = str(result.get("job_id", "")).strip()
        ui_print(f"Queued job {job_id} on session {session_id}: {cmd}")

        if job_id:
            state.register_pending_job(
                target_id=target_id,
                session_id=session_id,
                job_id=job_id,
                command=cmd,
            )

        if output_file:
            ui_print(
                "#F[...]F# is not applied automatically in queued mode. "
                "Save the result manually after it arrives."
            )

    if close_remote_on_exit:
        status, response = api.close_shell(target_id, session_id)
        stop_result_poller(target_id, session_id)
        state.forget_session_state(target_id, session_id)

        if status != 200:
            ui_print(f"[{status}] {response}")
        else:
            ui_print(f"Closed remote session: {session_id}")