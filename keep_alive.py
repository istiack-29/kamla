import logging
import os
import socket
from threading import Thread

from flask import Flask, jsonify

log = logging.getLogger("kamla.keepalive")

app = Flask('')

# Shared, honest status of the Discord connection. Updated by main.py.
# The web endpoint always answers 200 so uptime monitors / host health checks
# do not restart the process (restarts re-attempt login and can prolong a
# Discord-side block), but the body says what is actually going on.
status = {"state": "starting", "detail": ""}


def set_status(state: str, detail: str = "") -> None:
    status["state"] = state
    status["detail"] = detail


@app.route('/')
def home():
    if status["state"] == "connected":
        return "KAMLABot is alive!"
    return f"KAMLABot web server is up. Discord status: {status['state']}. {status['detail']}".strip()


@app.route('/health')
def health():
    return jsonify(status)


def _port() -> int:
    raw = os.getenv("PORT", "").strip()
    return int(raw) if raw.isdigit() else 10000


def port_in_use(port: int) -> bool:
    """True if something already listens on the keep-alive port. A second KAMLA
    process on the same host (same token => two gateway sessions) shows up here."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def run():
    app.run(host='0.0.0.0', port=_port())


def keep_alive():
    port = _port()
    if port_in_use(port):
        log.warning(
            "Port %d is already in use: another KAMLA process (or another service) is running "
            "on this host. Two processes with the same bot token cause duplicate event handling "
            "and extra API traffic. The web server in THIS process will not start.", port)
        return
    # daemon=True: the web thread must not keep the process alive after the bot stops.
    t = Thread(target=run, name="kamla-keepalive", daemon=True)
    t.start()
