from __future__ import annotations

from src.treeadmin.serv import run_server
from src.treeadmin.client import ping_server, interactive_shell
from src.treeadmin.config import ClientConfig


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


def _exec_menu() -> None:
    target_id = input("Target server node id [pc2]: ").strip() or "pc2"
    interactive_shell(target_id)


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
            print("2) Execute command")
            print("3) Reset config to basic")
            print("4) Show topology")
            print("0) Back")

            choice = input("> ").strip()

            if choice == "1":
                _ping_menu()
            elif choice == "2":
                _exec_menu()
            elif choice == "3":
                _reset_client_config()
            elif choice == "4":
                _show_topology_menu()
            elif choice == "0":
                break
            else:
                print("Unknown menu item")
    except KeyboardInterrupt:
        print("Close programm!\n")
        return



def main() -> None:
    try:
        while True:
            print("\nSelect mode:")
            print("1) Server")
            print("2) Client")
            print("0) Exit")

            choice = input("> ").strip()

            if choice == "1":
                run_server()
            elif choice == "2":
                _client_menu()
            elif choice == "0":
                print("Exit")
                break
            else:
                print("Unknown menu item")
    except KeyboardInterrupt:
        print("Close programm!\n")
        return
    