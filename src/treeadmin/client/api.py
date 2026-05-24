from __future__ import annotations

import http.client
import json
import logging
import socket
from pathlib import Path
from typing import Any

from src.treeadmin.config import ClientConfig
from src.treeadmin.routing import build_first_request

CONFIG_PATH = Path("config/config_client.json")

logger = logging.getLogger(__name__)


def load_client_config() -> ClientConfig:
    return ClientConfig.load(CONFIG_PATH)


def request(
    method: str,
    target_id: str,
    endpoint_path: str,
    *,
    payload: dict[str, Any] | None = None,
    extra_query: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> tuple[int, str]:
    try:
        client_config = load_client_config()

        host, port, url_path = build_first_request(
            client_config=client_config,
            target_node_id=target_id,
            endpoint_path=endpoint_path,
            extra_query=extra_query,
        )
    except Exception:
        # log
        logger.exception(
            "Client request prepare failed : method=%s target_id=%s endpoint_path=%s",
            method,
            target_id,
            endpoint_path
        )
        #
        raise

    body = b""
    headers: dict[str, str] = {}

    try:
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
    except Exception:
        # log
        logger.exception(
            "Client request payload encode failed : method=%s target_id=%s endpoint_path=%s",
            method,
            target_id,
            endpoint_path
        )
        #
        raise

    headers["Content-Length"] = str(len(body))
    effective_timeout = timeout if timeout is not None else client_config.timeout

    conn = http.client.HTTPConnection(
        host,
        port,
        timeout=effective_timeout,
    )

    try:
        conn.request(method, url_path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, raw
    except socket.timeout:
        # log
        logger.warning(
            "Client request timeout : method=%s target_id=%s endpoint_path=%s "
            "host=%s:%s timeout=%s",
            method,
            target_id,
            endpoint_path,
            host,
            port,
            effective_timeout
        )
        #
        return 408, "request timeout: server did not respond in time"
    
    except OSError as e:
        # log
        logger.warning(
            "Client connection error : method=%s target_id=%s endpoint_path=%s "
            "host=%s port=%s error=%s",
            method,
            target_id,
            endpoint_path,
            host,
            port,
            e
        )
        #
        return 503, f"connection error: {e}"
    
    except Exception as e:
        # log
        logger.exception(
            "Clietn request failed : method=%s target_id=%s endpoint_path=%s "
            "host=%s port=%s body_size=%s",
            method,
            target_id,
            endpoint_path,
            host,
            port,
            len(body)
        )
        #
        return 500, f"client error: {e}"
    
    finally:
        try:
            conn.close()
        except Exception:
            pass


def decode_json_response(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # log
        logger.warning(
            "Client invalid json response : response_size=%s",
            len(str(raw).encode("utf-8", errors="replace"))
        )
        #
        return None

    if not isinstance(data, dict):
        # log
        logger.warning(
            "Client invalid json response type : response_type=%s response_size=%s",
            type(data).__name__,
            len(str(raw).encode("utf-8", errors="replace"))
        )
        #
        return None

    return data


def get_configured_server_ids() -> list[str]:
    config = load_client_config()

    result: list[str] = []
    for node_id, node in config.nodes.items():
        host = node.get("host")
        port = node.get("port")

        if isinstance(host, str) and host.strip() and isinstance(port, int) and port > 0:
            result.append(node_id)

    return result


def ping_server(target_id: str, msg: str = "Hello, Server") -> tuple[int, str]:
    return request(
        "GET",
        target_id,
        "/hello",
        extra_query={"msg": msg},
    )


def open_shell_request(target_id: str) -> tuple[int, dict[str, Any] | str]:
    status, raw = request(
        "POST",
        target_id,
        "/open_shell",
        payload={},
    )

    if status != 200:
        return status, raw

    data = decode_json_response(raw)
    if data is None:
        return 502, f"invalid JSON from server: {raw}"

    return status, data


def list_sessions(target_id: str) -> list[dict[str, Any]] | None:
    status, raw = request("GET", target_id, "/list_sessions")

    if status != 200:
        return None

    data = decode_json_response(raw)
    if data is None:
        return None

    sessions = data.get("sessions", [])
    if not isinstance(sessions, list):
        # log
        logger.warning(
            "Client invalid sessions response : target_id=%s response_type=%s",
            target_id,
            type(sessions).__name__
        )
        #
        return None

    result: list[dict[str, Any]] = []
    for item in sessions:
        if isinstance(item, dict):
            result.append(item)

    return result


def send_queued_command(
    target_id: str,
    session_id: str,
    command: str,
) -> tuple[int, dict[str, Any]]:
    client_config = load_client_config()
    output_storage_format = client_config.preferred_storage_output_format

    status, raw = request(
        "POST",
        target_id,
        "/send_command",
        payload={
            "session_id": session_id,
            "command": command,
            "output_storage_format": output_storage_format,
        },
    )

    if status not in {200, 202}:
        return status, {"error": raw}

    data = decode_json_response(raw)
    if data is None:
        return 502, {"error": f"invalid JSON from server: {raw}"}

    return status, data


def get_session_jobs(target_id: str, session_id: str) -> dict[str, Any] | None:
    status, raw = request(
        "GET",
        target_id,
        "/session_jobs",
        extra_query={"session_id": session_id},
    )

    if status != 200:
        return None

    data = decode_json_response(raw)
    if data is None:
        return None

    return data


def get_results_summary(target_id: str, session_id: str | None = None) -> tuple[int, dict[str, Any] | str]:
    extra_query = {"session_id": session_id} if session_id else None

    status, raw = request(
        "GET",
        target_id,
        "/results_summary",
        extra_query=extra_query,
    )

    if status != 200:
        return status, raw

    data = decode_json_response(raw)
    if data is None:
        return 502, f"invalid JSON from server: {raw}"

    return status, data


def close_shell(target_id: str, session_id: str) -> tuple[int, str]:
    return request(
        "POST",
        target_id,
        "/close_shell",
        payload={"session_id": session_id},
    )


def pull_pending_results_raw(
    target_id: str,
    session_id: str | None = None,
) -> tuple[int, list[dict[str, Any]] | str]:
    query = {"session_id": session_id} if session_id else None

    status, raw = request(
        "GET",
        target_id,
        "/pull_pending_results",
        extra_query=query,
    )

    if status != 200:
        return status, raw

    data = decode_json_response(raw)
    if data is None:
        return 502, f"invalid JSON from server: {raw}"

    responses = data.get("responses", [])
    if not isinstance(responses, list):
        # log
        logger.warning(
            "Client invalid pending responses list : target_id=%s session_id=%s response_type=%s",
            target_id,
            session_id,
            type(responses).__name__
        )
        #
        return 502, "server returned invalid responses list"

    result: list[dict[str, Any]] = []
    for item in responses:
        if isinstance(item, dict):
            result.append(item)

    return 200, result


def ack_responses(target_id: str, response_ids: list[str]) -> tuple[int, str]:
    clean_ids = [str(item).strip() for item in response_ids if str(item).strip()]

    if not clean_ids:
        return 200, "nothing to ack"

    return request(
        "POST",
        target_id,
        "/ack_response",
        payload={"response_ids": clean_ids},
    )