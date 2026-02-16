# main.py
from src.treeadmin.serv import run_server
from src.treeadmin.client import run_client


def main():
    print("Select mode:")
    print("  1) Server")
    print("  2) Client")

    choice = input("> ").strip()
    host = "127.0.0.1"
    port = 8000

    if choice == "1":
        # host = input("Bind host (default 127.0.0.1): ").strip() or "127.0.0.1"
        # port = int(input("Port (default 8000): ").strip() or "8000")
        run_server(host, port)
        return

    if choice == "2":
        # host = input("Server host (default 127.0.0.1): ").strip() or "127.0.0.1"
        # port = int(input("Server port (default 8000): ").strip() or "8000")
        run_client(host, port)
        return

    print("Invalid selection.")
