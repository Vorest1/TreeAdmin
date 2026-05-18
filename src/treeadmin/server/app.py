from __future__ import annotations

import logging
import platform
from http.server import ThreadingHTTPServer
from pathlib import Path

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

    store = JobsStore(Path("data") / "server_store" / "state.json")
    logger.info("job storage started : %s", store.path)

    sessions = SessionManager(node_id=node_id)
    logger.info("session manager started")

    jobs = JobsManager(store=store, node_id=node_id)
    logger.info("jobs manager started")

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

    logger.info("server started http://%s:%s", host, port)
    print(f"Server started: http://{host}:{port}")

    jobs.recover_after_startup()
    cleanup.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        logger.info("server stopped")
