import logging
import platform
from pathlib import Path

try:
    from http.server import ThreadingHTTPServer
except ImportError:
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

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


def run_server(host="0.0.0.0", port=8000):
    # type: (str, int) -> None
    node_name = platform.node() or "node"
    node_id = "{}:{}".format(node_name, port)

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
    print("Server started: http://{}:{}".format(host, port))

    jobs.recover_after_startup()
    cleanup.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        logger.info("server stopped")