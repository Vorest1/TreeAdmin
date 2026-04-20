from __future__ import annotations

import http.client
import json
import threading
import time


class DeliveryService:
    def __init__(self, jobs, poll_interval_seconds: int = 5, timeout_seconds: int = 15) -> None:
        self.jobs = jobs
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        self._condition = threading.Condition()

    def start(self) -> None:
        thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="delivery-service",
        )
        thread.start()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _deliver_response(self, response: dict) -> bool:
        callback = response.get("client_callback")
        if not isinstance(callback, dict):
            return False

        host = callback.get("host")
        port = callback.get("port")
        path = callback.get("path", "/deliver_result")

        if not isinstance(host, str) or not host.strip():
            return False

        try:
            port = int(port)
        except Exception:
            return False

        if not isinstance(path, str) or not path.startswith("/"):
            path = "/deliver_result"

        body = json.dumps(response, ensure_ascii=False).encode("utf-8")
        conn = http.client.HTTPConnection(host, port, timeout=self.timeout_seconds)

        try:
            conn.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
            resp.read()
            return 200 <= resp.status < 300
        except Exception:
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _loop(self) -> None:
        while True:
            due = self.jobs.get_due_responses(time.time())

            if not due:
                with self._condition:
                    self._condition.wait(timeout=self.poll_interval_seconds)
                continue

            for item in due:
                response_id = str(item.get("response_id", ""))

                ok = self._deliver_response(item)
                if ok:
                    self.jobs.mark_delivery_success(response_id)
                else:
                    self.jobs.mark_delivery_failed(response_id)