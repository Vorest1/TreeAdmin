from src.treeadmin.serv import run_server
from src.treeadmin.client import run_client, send_config, interactive_shell
from src.treeadmin.config import ServerConfig

def main():
    print("Select mode:")
    print("  1) Server")
    print("  2) Client")

    choice = input("> ").strip()

    if choice == "1":
        choice_vars = ["y", "n", "Y", "N", ""]
        choice_cfg = "_"
        try:
            while choice_cfg not in choice_vars:
                choice_cfg = input("Do you want load current config [Y/n] (n - create new config)? > ").strip()
        except KeyboardInterrupt:
            print("Close programm")
            return
        
        if choice_cfg == "y" or choice_cfg == "Y" or choice_cfg == "" :
            run_server()
        if choice_cfg == "n" or choice_cfg == "N" :
            node_id = input("Enter Node Id: ").strip()
            host = input("Enter host: ").strip()
            port = int(input("Enter port: ").strip())
            server = ServerConfig.build(node_id=node_id, listen_host=host, listen_port=port)
            server.save()
            run_server(server)
        return

    if choice == "2":
        choice2 = 0
        try:
            while True:
                print("Enter client function: ")
                print("  1) ping")
                print("  2) Send config file")
                print("  3) Execute command")
                choice2 = input("> ").strip()

                if choice2 == "1":
                    run_client()

                if choice2 == "2":
                    target_id = input("Finally Server node id (default pc2): ").strip() or "pc2"
                    file_path = input("Config file path (default api/config.json): ").strip() or "api/config.json"
                    send_config(target_id, file_path)

                if choice2 == "3":
                    target_id = input("Finally Server node id (default pc2): ").strip() or "pc2"
                    interactive_shell(target_id)

        except KeyboardInterrupt:
            print("Client stopped")
            return

    print("Invalid selection.")
    return