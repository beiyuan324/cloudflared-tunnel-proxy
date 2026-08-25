#!/usr/bin/env python3
import json
import logging
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request


STOP = False


def env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def handle_signal(signum, _frame):
    global STOP
    STOP = True
    logging.info("received signal %s, stopping supervisor", signum)


def ready_connections(metrics_address):
    try:
        response = urllib.request.urlopen(
            f"http://{metrics_address}/ready", timeout=5
        )
        payload = json.loads(response.read().decode("utf-8"))
        return int(payload.get("readyConnections", 0))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
            return int(payload.get("readyConnections", 0))
        except (ValueError, json.JSONDecodeError, OSError):
            return 0
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def stop_child(child):
    if child.poll() is not None:
        return
    logging.warning("stopping cloudflared child pid=%s", child.pid)
    child.terminate()
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        logging.warning("cloudflared did not stop gracefully; killing pid=%s", child.pid)
        child.kill()
        child.wait(timeout=5)


def start_child(binary, token, metrics_address):
    command = [
        binary,
        "tunnel",
        "--no-autoupdate",
        "--metrics",
        metrics_address,
        "run",
        "--protocol",
        "http2",
        "--token",
        token,
    ]
    logging.info("starting cloudflared child")
    return subprocess.Popen(command)


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    binary = os.environ.get("CLOUDFLARED_BINARY", "/usr/local/bin/cloudflared")
    token = os.environ.get("TUNNEL_TOKEN", "")
    metrics_address = os.environ.get("METRICS_ADDRESS", "127.0.0.1:20241")
    check_interval = env_int("CHECK_INTERVAL_SECONDS", 10)
    startup_grace = env_int("STARTUP_GRACE_SECONDS", 120)
    unready_timeout = env_int("UNREADY_TIMEOUT_SECONDS", 90)

    if not token:
        raise RuntimeError("TUNNEL_TOKEN is required")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info(
        "supervisor started metrics=%s check_interval=%ss startup_grace=%ss unready_timeout=%ss",
        metrics_address,
        check_interval,
        startup_grace,
        unready_timeout,
    )

    retry_delay = 2
    while not STOP:
        child = start_child(binary, token, metrics_address)
        child_started = time.monotonic()
        unready_since = None
        restart_reason = "child exited"

        while not STOP:
            exit_code = child.poll()
            if exit_code is not None:
                restart_reason = f"child exited with code {exit_code}"
                break

            now = time.monotonic()
            connections = ready_connections(metrics_address)
            within_grace = now - child_started < startup_grace
            if connections is None or connections <= 0:
                if not within_grace:
                    if unready_since is None:
                        unready_since = now
                    elif now - unready_since >= unready_timeout:
                        restart_reason = (
                            f"readyConnections stayed at 0 for {unready_timeout}s"
                        )
                        break
            else:
                if unready_since is not None:
                    logging.info("cloudflared recovered with %s ready connections", connections)
                unready_since = None
                retry_delay = 2

            time.sleep(check_interval)

        if STOP:
            stop_child(child)
            break

        logging.warning("restarting cloudflared: %s", restart_reason)
        stop_child(child)
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 30)


if __name__ == "__main__":
    main()
