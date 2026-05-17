from __future__ import annotations

import platform
from http.server import ThreadingHTTPServer
from pathlib import Path
import logging

from src.treeadmin.server.cleanup import CleanupService
from src.treeadmin.server.handler import ProxyHandler
from src.treeadmin.server.jobs_manager import JobsManager
from src.treeadmin.server.jobs_store import JobsStore
from src.treeadmin.server.proxy import ProxySupport
from src.treeadmin.server.sessions import SessionManager
from src.treeadmin.server.workers import SessionWorkers

logger = logging.getLogger(__name__)

class TreeAdminHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def run_server(host: str = "0.0.0.0", port: int = 8000):
    node_name = platform.node() or "node"
    node_id = f"{node_name}:{port}"

    store = JobsStore(Path("data") / "server_jobs.json")
    logger.info("Job storage started : %s", "data/server_jobs.json")

    sessions = SessionManager(node_id=node_id)
    logger.info("Session manager started")

    jobs = JobsManager(store=store, node_id=node_id)
    logger.info("Jobs manager started")

    workers = SessionWorkers(sessions=sessions, jobs=jobs)
    proxy = ProxySupport()
    cleanup = CleanupService(sessions=sessions, jobs=jobs, store=store)

    httpd = TreeAdminHTTPServer((host, port), ProxyHandler)
    httpd.node_id = node_id
    httpd.store = store
    httpd.sessions = sessions
    httpd.jobs = jobs
    httpd.workers = workers
    httpd.proxy = proxy

    logger.info("Server started http://%s:%s", host, port)
    print(f"Server started: http://{host}:{port}")

    jobs.mark_startup_orphans()
    cleanup.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        logger.info("Server stopped")