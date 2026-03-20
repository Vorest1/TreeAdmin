from src.treeadmin.serv import run_server
from src.treeadmin.client import run_client, send_config, interactive_shell

def main():
    print("Select mode:")
    print("  1) Server")
    print("  2) Client")

    choice = input("> ").strip()
    host = "127.0.0.1"
    port = 8000

    if choice == "1":
        host = input("Server host (default 127.0.0.1): ").strip() or "127.0.0.1"
        port = int(input("Server port (default 8000): ").strip() or "8000")
        run_server(host, port)
        return

    if choice == "2":
        host = input("Server host (default 127.0.0.1): ").strip() or "127.0.0.1"
        port = int(input("Server port (default 8000): ").strip() or "8000")
        choice2 = 0
        try:
            while True:
                print("Enter client function: ")
                print("  1) ping")
                print("  2) Send config file")
                print("  3) Execute command")
                choice2 = input("> ").strip()
                if choice2 == "1":
                    run_client(host, port)
                if choice2 == "2":
                    to_port = int(input("Finally Server port (default 8000): ").strip() or "8000")
                    send_config(host, port, to_port)
                if choice2 == "3":
                    to_port = int(input("Finally Server port (default 8000): ").strip() or "8000")
                    interactive_shell(host, port, to_port)
        except KeyboardInterrupt:
            print("Client stopped")
            return

    print("Invalid selection.")
