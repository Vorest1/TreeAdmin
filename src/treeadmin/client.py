import socket

connection = socket.socket(socket.AF_INET, socket. SOCK_STREAM)
IP = "127.0.0.1"
PORT = 80
connection.connect((IP, PORT))
rd = connection.recv(1024)
print(rd.decode('utf8'))
connection.send("Hello, Server!".encode('utf8'))
connection.close()