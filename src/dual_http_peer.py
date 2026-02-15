#!/usr/bin/env python3
"""One app that works as both HTTP server and client for peer-to-peer greetings."""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request


class MessageHandler(BaseHTTPRequestHandler):
    """Handles incoming HTTP greeting messages."""

    server_version = "DualHttpPeer/1.0"

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler naming)
        if self.path != "/message":
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"

        try:
            payload = json.loads(raw_body.decode("utf-8"))
            incoming_message = payload.get("message", "")
        except (ValueError, UnicodeDecodeError):
            self.send_error(400, "Invalid JSON payload")
            return

        if incoming_message == "Hello, server!":
            response_message = "Hello, Client!"
        elif incoming_message == "Hello, Client!":
            response_message = "Hello, server!"
        else:
            response_message = "Unknown message"

        response_payload = {
            "received": incoming_message,
            "response": response_message,
            "listener_port": self.server.server_address[1],
        }

        encoded = json.dumps(response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

        print(
            f"[SERVER:{self.server.server_address[1]}] Received: '{incoming_message}'. "
            f"Responded: '{response_message}'."
        )

    def log_message(self, fmt: str, *args: object) -> None:
        # Suppress default verbose logging in favor of cleaner custom logs.
        return


def run_server(listen_host: str, listen_port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((listen_host, listen_port), MessageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[SERVER:{listen_port}] Listening on http://{listen_host}:{listen_port}")
    return server


def send_message(peer_host: str, peer_port: int, message: str, retries: int, delay: float) -> None:
    url = f"http://{peer_host}:{peer_port}/message"
    body = json.dumps({"message": message}).encode("utf-8")

    for attempt in range(1, retries + 1):
        req = request.Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            print(
                f"[CLIENT->{peer_port}] Sent: '{message}'. "
                f"Peer replied: '{payload.get('response')}'."
            )
            return
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(
                f"[CLIENT->{peer_port}] Attempt {attempt}/{retries} failed: {exc}. "
                f"Retrying in {delay} sec..."
            )
            time.sleep(delay)

    raise RuntimeError(f"Could not deliver message to {url} after {retries} attempts")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Start one peer instance (HTTP server + client). "
            "Run two identical instances on different ports to exchange greetings."
        )
    )
    parser.add_argument("--listen-host", default="127.0.0.1", help="Host interface for HTTP server")
    parser.add_argument("--listen-port", type=int, default=80, help="Port for local HTTP server")
    parser.add_argument("--peer-host", default="127.0.0.1", help="Peer host")
    parser.add_argument("--peer-port", type=int, default=8080, help="Peer server port")
    parser.add_argument("--message", default="Hello, server!", help="Message sent to peer")
    parser.add_argument("--retries", type=int, default=20, help="Retry attempts while waiting for peer")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="Delay between retries in seconds")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = run_server(args.listen_host, args.listen_port)

    # Give both peers time to start before first client request.
    time.sleep(0.5)

    send_message(args.peer_host, args.peer_port, args.message, args.retries, args.retry_delay)

    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
