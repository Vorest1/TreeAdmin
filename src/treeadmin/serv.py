import socket

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
IP = "127.0.0.1"
PORT = 8080

listener.bind((IP, PORT))
listener.listen(1)

connection, address = listener.accept()
connection.send("Hello, Client!".encode("utf8"))

while True:
    data = connection.recv(1024)
    if not data:
        break
    print(data.decode("utf8"))

connection.close()
listener.close()
