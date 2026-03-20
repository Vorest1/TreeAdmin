#config class
import json
from pathlib import Path

# conf= {
#     "id": "1",
#     "ip": "127.0.0.1",
#     "port": 8000,
#     "next_hop":[ 
#         {"ip":"127.0.0.1", "port":8001},
#         {"ip":"127.0.0.2", "port":8002},
#         {"ip":"127.0.0.3", "port":8003}
#     ]
# }

file_path = Path("api/config.json")

class Config:
    
    # def __init___(self):
    #     self.id = 0
    #     self.ip = "127.0.0.1"
    #     self.port = 8000
    #     self.next_hop = [{str, int}]
    
    def __init__(self, conf : dict):
        self.id = conf["id"] or 0
        self.ip = conf["ip"] or "127.0.0.1"
        self.port = conf["port"] or 8000

        self.next_hop = []
        for hop in conf["next_hop"]:
            self.next_hop.append([hop["ip"], hop["port"]])

        json.dump(file_path, ensure_ascii=False).encode("utf-8")
        return self
    
    def GetConfig(self, conf: dict):
        if not file_path.exists():
            print("Config file not found")
            return
        
        config = json.loads(file_path.read_text(encoding="utf-8"))

        return config
    
    def SetNextHop(self, ip: str, port: int):
        self.next_hop.append([ip, port])
 