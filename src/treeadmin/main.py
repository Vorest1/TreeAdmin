from __future__ import annotations

from src.treeadmin.client import close_shell, interactive_shell, list_sessions, ping_server
from src.treeadmin.config import ClientConfig
from src.treeadmin.serv import run_server


def _print_topology(config: ClientConfig) -> None:
    print("\nCurrent topology:")
    print(f"root_id: {config.root_id}")
    print(f"timeout: {config.timeout}")

    default_port = config.data.get("default_port")
    if default_port is not None:
        print(f"default_port: {default_port}")

    print("nodes:")
    for node_id, node in config.nodes.items():
        host = node.get("host", "-")
        port = node.get("port", config.data.get("default_port", "-"))
        children = node.get("children", [])
        print(f"  {node_id}: host={host}, port={port}, children={children}")


def _reset_client_config() -> None:
    config = ClientConfig.build_basic()
    config.save()
    print(f"Client config reset: {config.path}")


def _ping_menu() -> None:
    target_id = input("Target server node id [pc2]: ").strip() or "pc2"
    status, body = ping_server(target_id)
    print("CLIENT: sent REQUEST GET: Hello, Server")
    print(f"CLIENT: GET: {body.strip()} (status={status})")


def _open_shell_menu() -> None:
    target_id = input("Target server node id [pc2]: ").strip() or "pc2"
    interactive_shell(target_id)


def _attach_shell_menu() -> None:
    target_id = input("Target server node id [pc2]: ").strip() or "pc2"
    sessions = list_sessions(target_id)
    if sessions is None:
        return
    if not sessions:
        print("No active sessions on server")
        return

    print("Active sessions:")
    for item in sessions:
        print(f"  {item.get('session_id')}: cwd={item.get('cwd', '')}")

    session_id = input("Session id to attach: ").strip()
    if not session_id:
        print("Empty session id")
        return

    cwd = ""
    for item in sessions:
        if str(item.get("session_id")) == session_id:
            cwd = str(item.get("cwd", ""))
            break

    interactive_shell(target_id, existing_session_id=session_id, existing_cwd=cwd)


def _list_sessions_menu() -> None:
    target_id = input("Target server node id [pc2]: ").strip() or "pc2"
    sessions = list_sessions(target_id)
    if sessions is None:
        return
    if not sessions:
        print("No active sessions on server")
        return

    print("\nActive sessions:")
    for item in sessions:
        print(
            f"  session_id={item.get('session_id')} platform={item.get('platform')} cwd={item.get('cwd', '')}"
        )


def _close_session_menu() -> None:
    target_id = input("Target server node id [pc2]: ").strip() or "pc2"
    session_id = input("Session id to close: ").strip()
    if not session_id:
        print("Empty session id")
        return

    status, body = close_shell(target_id, session_id)
    print(f"[{status}] {body}")


def _show_topology_menu() -> None:
    try:
        config = ClientConfig.load()
    except Exception as e:
        print(f"Failed to load client config: {e}")
        return

    _print_topology(config)


def _client_menu() -> None:
    try:
        while True:
            print("\nEnter client function:")
            print("1) Ping server")
            print("2) Open new shell session")
            print("3) Attach to existing shell session")
            print("4) List active server sessions")
            print("5) Close remote session")
            print("6) Reset config to basic")
            print("7) Show topology")
            print("0) Back")

            choice = input("> ").strip()

            if choice == "1":
                _ping_menu()
            elif choice == "2":
                _open_shell_menu()
            elif choice == "3":
                _attach_shell_menu()
            elif choice == "4":
                _list_sessions_menu()
            elif choice == "5":
                _close_session_menu()
            elif choice == "6":
                _reset_client_config()
            elif choice == "7":
                _show_topology_menu()
            elif choice == "0":
                break
            else:
                print("Unknown menu item")
    except KeyboardInterrupt:
        print("Close programm!\n")


def main() -> None:
    while True:
        print("\nSelect mode:")
        print("1) Server")
        print("2) Client")
        print("0) Exit")

        choice = input("> ").strip()

        try:
            if choice == "1":
                run_server()
                return
            if choice == "2":
                _client_menu()
                return
            if choice == "0":
                print("Exit")
                return

            print("Unknown menu item")
        except KeyboardInterrupt:
            print("Close programm!\n")
            return
