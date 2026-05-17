from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from src.treeadmin.client import api
from src.treeadmin.client import state
from src.treeadmin.client.results import format_response_payload
from src.treeadmin.client.terminal import ui_input, ui_print


_LAST_DASHBOARD: dict[str, Any] | None = None


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _is_failed_summary(item: dict[str, Any]) -> bool:
    if item.get("has_error") is True:
        return True

    status = str(item.get("status", "")).lower()
    if status in {"failed", "canceled", "orphaned"}:
        return True

    returncode = item.get("returncode")
    if isinstance(returncode, int) and returncode != 0:
        return True

    if item.get("error"):
        return True

    return False


def _visible_responses(target_id: str, responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for item in responses:
        response_id = str(item.get("response_id", "")).strip()
        session_id = str(item.get("session_id", "")).strip()

        if not response_id:
            continue

        if session_id and state.is_session_active(target_id, session_id):
            continue

        if state.is_response_ignored(target_id, response_id):
            continue

        result.append(item)

    return result


def _scan_one_server(target_id: str) -> dict[str, Any]:
    status, result = api.get_results_summary(target_id)

    if status != 200 or not isinstance(result, dict):
        return {
            "target_id": target_id,
            "online": False,
            "error": str(result),
            "summary": None,
        }

    return {
        "target_id": target_id,
        "online": True,
        "error": None,
        "summary": result,
    }


def _get_scan_targets_from_history() -> list[str]:
    targets = sorted(state.get_notification_target_ids())
    return targets


def scan_dashboard() -> dict[str, Any]:
    global _LAST_DASHBOARD

    targets = _get_scan_targets_from_history()
    servers: list[dict[str, Any]] = []

    if targets:
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as executor:
            futures = {
                executor.submit(_scan_one_server, target_id): target_id
                for target_id in targets
            }

            for future in as_completed(futures):
                target_id = futures[future]
                try:
                    servers.append(future.result())
                except Exception as e:
                    servers.append(
                        {
                            "target_id": target_id,
                            "online": False,
                            "error": str(e),
                            "summary": None,
                        }
                    )

    servers.sort(key=lambda item: str(item.get("target_id", "")))

    snapshot = {
        "created_at": _utc_now(),
        "servers": servers,
        "error": None,
    }

    _LAST_DASHBOARD = snapshot
    state.set_last_dashboard_scan_now()
    return snapshot


def get_last_dashboard_or_scan() -> dict[str, Any]:
    if _LAST_DASHBOARD is None:
        return scan_dashboard()
    return _LAST_DASHBOARD


def _server_counts(entry: dict[str, Any]) -> dict[str, int]:
    if not entry.get("online"):
        return {
            "ready": 0,
            "failed": 0,
            "running": 0,
            "queued": 0,
        }

    target_id = str(entry.get("target_id", ""))
    summary = entry.get("summary") or {}

    responses = summary.get("responses", [])
    if not isinstance(responses, list):
        responses = []

    visible = _visible_responses(target_id, responses)
    failed = [item for item in visible if _is_failed_summary(item)]

    return {
        "ready": len(visible),
        "failed": len(failed),
        "running": int(summary.get("running_count", 0) or 0),
        "queued": int(summary.get("queued_count", 0) or 0),
    }


def get_unread_notification_count(snapshot: dict[str, Any] | None = None) -> int:
    if snapshot is None:
        snapshot = get_last_dashboard_or_scan()

    servers = snapshot.get("servers", [])
    if not isinstance(servers, list):
        return 0

    total = 0
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        total += _server_counts(entry)["ready"]

    return total


def print_dashboard(snapshot: dict[str, Any] | None = None) -> None:
    if snapshot is None:
        snapshot = get_last_dashboard_or_scan()

    if snapshot.get("error"):
        ui_print(f"Dashboard error: {snapshot['error']}")
        return

    servers = snapshot.get("servers", [])
    if not isinstance(servers, list) or not servers:
        ui_print(
            "\nDashboard:\n"
            "  No command-history servers yet.\n"
            "  Servers will appear here after you send commands from this client."
        )
        return

    lines: list[str] = [
        "\nDashboard:",
        f"scan_at: {snapshot.get('created_at')}",
        "",
    ]

    active_lines: list[str] = []
    idle_count = 0

    for entry in servers:
        target_id = str(entry.get("target_id", ""))

        if not entry.get("online"):
            active_lines.append(
                f"  {target_id:<16} offline  {entry.get('error', 'connection error')}"
            )
            continue

        counts = _server_counts(entry)

        has_activity = any(
            counts[key] > 0
            for key in {"ready", "failed", "running", "queued"}
        )

        if not has_activity:
            idle_count += 1
            continue

        active_lines.append(
            f"  {target_id:<16} online   "
            f"unread={counts['ready']} "
            f"failed={counts['failed']} "
            f"running={counts['running']} "
            f"queued={counts['queued']}"
        )

    if active_lines:
        lines.extend(active_lines)
    else:
        lines.append("  No unread/running/queued results")

    if idle_count:
        lines.append("")
        lines.append(f"  idle history servers hidden: {idle_count}")

    ui_print("\n".join(lines))


def refresh_dashboard() -> dict[str, Any]:
    snapshot = scan_dashboard()
    print_dashboard(snapshot)
    return snapshot


def refresh_notifications() -> dict[str, Any]:
    return scan_dashboard()


def show_dashboard() -> None:
    print_dashboard(get_last_dashboard_or_scan())


def _collect_response_summaries(
    *,
    failed_only: bool = False,
) -> list[dict[str, Any]]:
    snapshot = get_last_dashboard_or_scan()
    servers = snapshot.get("servers", [])

    result: list[dict[str, Any]] = []

    if not isinstance(servers, list):
        return result

    for entry in servers:
        target_id = str(entry.get("target_id", ""))
        if not entry.get("online"):
            continue

        summary = entry.get("summary") or {}
        responses = summary.get("responses", [])
        if not isinstance(responses, list):
            continue

        for item in _visible_responses(target_id, responses):
            if failed_only and not _is_failed_summary(item):
                continue

            result.append(
                {
                    "target_id": target_id,
                    "response_id": str(item.get("response_id", "")),
                    "job_id": str(item.get("job_id", "")),
                    "session_id": str(item.get("session_id", "")),
                    "command": str(item.get("command", "")),
                    "status": str(item.get("status", "")),
                    "returncode": item.get("returncode"),
                    "ready_at": item.get("ready_at"),
                    "failed": _is_failed_summary(item),
                }
            )

    return result


def _collect_notification_servers(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    servers = snapshot.get("servers", [])
    if not isinstance(servers, list):
        return []

    result: list[dict[str, Any]] = []

    for entry in servers:
        if not isinstance(entry, dict):
            continue

        target_id = str(entry.get("target_id", ""))
        counts = _server_counts(entry)

        if counts["ready"] <= 0:
            continue

        result.append(
            {
                "target_id": target_id,
                "online": bool(entry.get("online")),
                "counts": counts,
                "entry": entry,
            }
        )

    result.sort(key=lambda item: str(item.get("target_id", "")))
    return result


def _print_notification_servers(items: list[dict[str, Any]]) -> None:
    lines = ["\nServers with unread responses:"]

    for index, item in enumerate(items, start=1):
        target_id = str(item.get("target_id", ""))
        counts = item.get("counts", {})
        unread = int(counts.get("ready", 0) or 0)
        failed = int(counts.get("failed", 0) or 0)

        failed_text = f", failed={failed}" if failed else ""
        lines.append(f"  {index}) {target_id} — unread={unread}{failed_text}")

    lines.append("  0) Back")
    ui_print("\n".join(lines))


def _get_visible_full_responses_for_server(target_id: str) -> list[dict[str, Any]]:
    status, raw_items = api.pull_pending_results_raw(target_id)

    if status != 200 or not isinstance(raw_items, list):
        ui_print(f"[{status}] failed to load full results from {target_id}: {raw_items}")
        return []

    visible: list[dict[str, Any]] = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        response_id = str(item.get("response_id", "")).strip()
        session_id = str(item.get("session_id", "")).strip()

        if not response_id:
            continue

        if session_id and state.is_session_active(target_id, session_id):
            continue

        if state.is_response_ignored(target_id, response_id):
            continue

        visible.append(item)

    return visible


def _show_server_notifications(target_id: str) -> None:
    responses = _get_visible_full_responses_for_server(target_id)

    if not responses:
        ui_print(f"\nNo unread responses on {target_id}")
        return

    displayed_response_ids: list[str] = []

    for response in responses:
        ui_print(format_response_payload(response))

        response_id = str(response.get("response_id", "")).strip()
        job_id = str(response.get("job_id", "")).strip()

        if response_id:
            displayed_response_ids.append(response_id)

        if job_id:
            state.mark_pending_job_done(target_id, job_id)

        session_id = str(response.get("session_id", "")).strip()
        cwd = str(response.get("cwd", "")).strip()
        if session_id and cwd:
            state.set_session_cwd(target_id, session_id, cwd)

    if not displayed_response_ids:
        return

    ui_print(
        "\nAction for displayed responses:\n"
        "1) Mark as read/delete from server\n"
        "2) Ignore locally, keep on server\n"
        "0) Keep unread"
    )

    choice = ui_input("> ").strip()

    if choice == "1":
        status, raw = api.ack_responses(target_id, displayed_response_ids)
        if status != 200:
            ui_print(f"[{status}] failed to ack responses on {target_id}: {raw}")
        else:
            ui_print(f"{target_id}: marked as read/deleted from server: {len(displayed_response_ids)}")

        refresh_notifications()
        return

    if choice == "2":
        state.ignore_responses(target_id, displayed_response_ids)
        ui_print(f"{target_id}: ignored locally: {len(displayed_response_ids)}")
        refresh_notifications()
        return

    ui_print("Responses kept unread")


def show_notifications() -> None:
    snapshot = refresh_notifications()
    servers = _collect_notification_servers(snapshot)

    if not servers:
        ui_print("\nNo unread notifications")
        return

    while True:
        _print_notification_servers(servers)

        raw = ui_input("Server number: ").strip()

        if raw == "0":
            return

        try:
            index = int(raw)
        except ValueError:
            ui_print("Invalid menu item")
            continue

        if index < 1 or index > len(servers):
            ui_print("Invalid menu item")
            continue

        target_id = str(servers[index - 1].get("target_id", ""))
        if not target_id:
            ui_print("Invalid server id")
            continue

        _show_server_notifications(target_id)

        snapshot = refresh_notifications()
        servers = _collect_notification_servers(snapshot)

        if not servers:
            ui_print("\nNo unread notifications")
            return


def _print_response_summary_list(items: list[dict[str, Any]]) -> None:
    if not items:
        ui_print("\nNo ready results")
        return

    lines = ["\nReady results:"]

    for index, item in enumerate(items, start=1):
        failed_mark = "FAILED" if item.get("failed") else "OK"
        rc = item.get("returncode")
        rc_text = f" rc={rc}" if isinstance(rc, int) else ""
        lines.append(
            f"  {index}) [{item['target_id']}][response {item['response_id']}]"
            f"[job {item['job_id']}][session {item['session_id']}] "
            f"{failed_mark}{rc_text} :: {item['command']}"
        )

    ui_print("\n".join(lines))


def _parse_selection(raw: str, max_index: int) -> list[int]:
    raw = raw.strip().lower()

    if not raw:
        return list(range(1, max_index + 1))

    if raw in {"0", "back", "b"}:
        return []

    if raw in {"all", "*"}:
        return list(range(1, max_index + 1))

    result: list[int] = []

    for part in raw.replace(",", " ").split():
        try:
            index = int(part)
        except ValueError:
            continue

        if 1 <= index <= max_index:
            result.append(index)

    return sorted(set(result))


def show_ready_results(*, failed_only: bool = False) -> None:
    refresh_notifications()
    items = _collect_response_summaries(failed_only=failed_only)

    if not items:
        ui_print("\nNo failed results" if failed_only else "\nNo ready results")
        return

    _print_response_summary_list(items)

    raw = ui_input(
        "\nSelect results to open [Enter=all, numbers='1 3', 0=back]: "
    ).strip()

    indexes = _parse_selection(raw, len(items))
    if not indexes:
        return

    selected = [items[index - 1] for index in indexes]

    by_target: dict[str, list[str]] = {}
    for item in selected:
        target_id = str(item.get("target_id", ""))
        response_id = str(item.get("response_id", ""))

        if target_id and response_id:
            by_target.setdefault(target_id, []).append(response_id)

    displayed_by_target: dict[str, list[str]] = {}

    for target_id, response_ids in by_target.items():
        full_items = _get_visible_full_responses_for_server(target_id)

        for response in full_items:
            response_id = str(response.get("response_id", "")).strip()
            if response_id not in response_ids:
                continue

            ui_print(format_response_payload(response))
            displayed_by_target.setdefault(target_id, []).append(response_id)

            job_id = str(response.get("job_id", "")).strip()
            if job_id:
                state.mark_pending_job_done(target_id, job_id)

    if not displayed_by_target:
        return

    ui_print(
        "\nAction for displayed results:\n"
        "  r  mark as read/delete from server\n"
        "  i  ignore locally, keep on server\n"
        "  Enter  keep unread"
    )

    action = ui_input("> ").strip().lower()

    if action in {"r", "read", "delete", "d"}:
        for target_id, response_ids in displayed_by_target.items():
            status, raw = api.ack_responses(target_id, response_ids)
            if status != 200:
                ui_print(f"[{status}] failed to ack responses on {target_id}: {raw}")
            else:
                ui_print(f"{target_id}: deleted from pending results: {len(response_ids)}")

        refresh_notifications()
        return

    if action in {"i", "ignore"}:
        for target_id, response_ids in displayed_by_target.items():
            state.ignore_responses(target_id, response_ids)
            ui_print(f"{target_id}: ignored locally: {len(response_ids)}")

        refresh_notifications()
        return

    ui_print("Results kept unread")


def show_failed_results() -> None:
    show_ready_results(failed_only=True)


def dashboard_menu() -> None:
    while True:
        snapshot = refresh_notifications()
        unread = get_unread_notification_count(snapshot)

        ui_print(
            "\nNotification actions:\n"
            f"1) View notifications [{unread}]\n"
            "2) Refresh notification summary\n"
            "0) Back"
        )

        choice = ui_input("> ").strip()

        if choice == "1":
            show_notifications()
        elif choice == "2":
            print_dashboard(refresh_notifications())
        elif choice == "0":
            return
        else:
            ui_print("Unknown menu item")