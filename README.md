# Dual HTTP peer (client + server in one app)

Скрипт `dual_http_peer.py` запускает HTTP-сервер и одновременно отправляет HTTP-сообщение другому экземпляру **этого же** скрипта.

## Сообщения
- исходящее: `Hello, server!`
- ответ: `Hello, Client!`

Оба сообщения участвуют в обмене автоматически: один экземпляр отправляет `Hello, server!`, второй отвечает `Hello, Client!`.

## Запуск двух одинаковых приложений на localhost

Терминал 1 (порт `80`, отправка на `8080`):

```bash
python3 dual_http_peer.py --listen-port 80 --peer-port 8080
```

Терминал 2 (порт `8080`, отправка на `80`):

```bash
python3 dual_http_peer.py --listen-port 8080 --peer-port 80
```

После старта экземпляры обменяются сообщениями по HTTP на `localhost`.
