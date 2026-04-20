from __future__ import annotations

import platform
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.treeadmin.server.cleanup import CleanupService
from src.treeadmin.server.delivery import DeliveryService
from src.treeadmin.server.handler import ProxyHandler
from src.treeadmin.server.jobs_service import JobService
from src.treeadmin.server.jobs_store import JobsStore
from src.treeadmin.server.proxy import ProxySupport
from src.treeadmin.server.sessions import SessionManager
from src.treeadmin.server.workers import SessionWorkers


class TreeAdminHTTPServer(ThreadingHTTPServer):
    pass


def run_server(host: str = "0.0.0.0", port: int = 8000):
    node_name = platform.node() or "node"
    node_id = f"{node_name}:{port}"

    store = JobsStore(Path("data") / "server_jobs.json")
    sessions = SessionManager(node_id=node_id)
    jobs = JobService(store=store, node_id=node_id)
    workers = SessionWorkers(sessions=sessions, jobs=jobs)
    proxy = ProxySupport()
    delivery = DeliveryService(jobs=jobs)
    cleanup = CleanupService(sessions=sessions, jobs=jobs, store=store)

    jobs.set_notifier(delivery.wake)

    httpd = TreeAdminHTTPServer((host, port), ProxyHandler)
    httpd.node_id = node_id
    httpd.store = store
    httpd.sessions = sessions
    httpd.jobs = jobs
    httpd.workers = workers
    httpd.proxy = proxy
    httpd.delivery = delivery

    print(f"Server started: http://{host}:{port}")

    jobs.mark_startup_orphans()
    delivery.start()
    cleanup.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")