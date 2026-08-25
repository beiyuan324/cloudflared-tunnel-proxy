#!/usr/bin/env python3
import logging
import os
import selectors
import socket
import threading
import time


BUFFER_SIZE = 64 * 1024
MAX_INITIAL_BYTES = 64 * 1024
CONNECT_TIMEOUT = 15
DEFAULT_TARGET = "region1.v2.argotunnel.com"
ALLOWED_TARGETS = {
    "region1.v2.argotunnel.com",
    "region2.v2.argotunnel.com",
}


def extract_sni(data):
    if len(data) < 5 or data[0] != 22:
        return None

    record_length = int.from_bytes(data[3:5], "big")
    if len(data) < 5 + record_length:
        return None

    body = data[5 : 5 + record_length]
    if len(body) < 4 or body[0] != 1:
        return None

    hello_length = int.from_bytes(body[1:4], "big")
    if len(body) < 4 + hello_length:
        return None

    end = 4 + hello_length
    pos = 4
    if pos + 2 + 32 > end:
        return None
    pos += 2 + 32

    if pos >= end:
        return None
    session_id_length = body[pos]
    pos += 1 + session_id_length
    if pos + 2 > end:
        return None

    cipher_suites_length = int.from_bytes(body[pos : pos + 2], "big")
    pos += 2 + cipher_suites_length
    if pos >= end:
        return None

    compression_length = body[pos]
    pos += 1 + compression_length
    if pos + 2 > end:
        return None

    extensions_length = int.from_bytes(body[pos : pos + 2], "big")
    pos += 2
    extensions_end = min(pos + extensions_length, end)

    while pos + 4 <= extensions_end:
        extension_type = int.from_bytes(body[pos : pos + 2], "big")
        extension_length = int.from_bytes(body[pos + 2 : pos + 4], "big")
        pos += 4
        extension_end = pos + extension_length
        if extension_end > extensions_end:
            return None

        if extension_type == 0 and extension_length >= 5:
            names_length = int.from_bytes(body[pos : pos + 2], "big")
            name_pos = pos + 2
            names_end = min(name_pos + names_length, extension_end)
            while name_pos + 3 <= names_end:
                name_type = body[name_pos]
                name_length = int.from_bytes(body[name_pos + 1 : name_pos + 3], "big")
                name_pos += 3
                name_end = name_pos + name_length
                if name_end > names_end:
                    return None
                if name_type == 0:
                    try:
                        return body[name_pos:name_end].decode("ascii").lower()
                    except UnicodeDecodeError:
                        return None
                name_pos = name_end

        pos = extension_end

    return None


def read_initial(client):
    data = bytearray()
    client.settimeout(CONNECT_TIMEOUT)
    while len(data) < MAX_INITIAL_BYTES:
        chunk = client.recv(min(4096, MAX_INITIAL_BYTES - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        server_name = extract_sni(data)
        if server_name:
            target = server_name if server_name in ALLOWED_TARGETS else DEFAULT_TARGET
            return bytes(data), target
        if len(data) >= 5 and data[0] != 22:
            break
        if len(data) >= 5:
            record_length = int.from_bytes(data[3:5], "big")
            if len(data) >= 5 + record_length:
                break
    return bytes(data), DEFAULT_TARGET


def connect_through_proxy(proxy_host, proxy_port, target, target_port):
    upstream = socket.create_connection((proxy_host, proxy_port), timeout=CONNECT_TIMEOUT)
    request = (
        f"CONNECT {target}:{target_port} HTTP/1.1\r\n"
        f"Host: {target}:{target_port}\r\n"
        "Proxy-Connection: Keep-Alive\r\n"
        "\r\n"
    ).encode("ascii")
    upstream.sendall(request)

    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = upstream.recv(4096)
        if not chunk:
            raise ConnectionError("proxy closed before CONNECT response")
        response.extend(chunk)
        if len(response) > 64 * 1024:
            raise ConnectionError("proxy CONNECT response is too large")

    status_line = bytes(response).split(b"\r\n", 1)[0]
    if b" 200 " not in status_line:
        raise ConnectionError(f"proxy CONNECT failed: {status_line[:160]!r}")

    upstream.settimeout(None)
    return upstream


def relay(client, upstream):
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    try:
        while True:
            events = selector.select()
            for key, _ in events:
                data = key.fileobj.recv(BUFFER_SIZE)
                if not data:
                    return
                key.data.sendall(data)
    finally:
        selector.close()


def handle_client(client, address, proxy_host, proxy_port, target_port):
    upstream = None
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        initial, target = read_initial(client)
        upstream = connect_through_proxy(proxy_host, proxy_port, target, target_port)
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if initial:
            upstream.sendall(initial)
        client.settimeout(None)
        relay(client, upstream)
    except Exception as error:
        logging.warning("connection %s failed: %s", address, error)
    finally:
        for connection in (client, upstream):
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()


def serve_listener(listen_ip, listen_port, proxy_host, proxy_port, target_port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((listen_ip, listen_port))
    listener.listen(128)
    logging.info(
        "listening on %s:%s, forwarding via %s:%s",
        listen_ip,
        listen_port,
        proxy_host,
        proxy_port,
    )

    while True:
        client, address = listener.accept()
        thread = threading.Thread(
            target=handle_client,
            args=(client, address, proxy_host, proxy_port, target_port),
            daemon=True,
        )
        thread.start()


def main():
    listen_ips_raw = os.environ.get("LISTEN_IPS", "").strip()
    if listen_ips_raw:
        listen_ips = [ip.strip() for ip in listen_ips_raw.split(",") if ip.strip()]
    else:
        listen_ips = [os.environ.get("LISTEN_HOST", "127.0.0.1")]
    listen_port = int(os.environ.get("LISTEN_PORT", "7844"))
    proxy_host = os.environ.get("PROXY_HOST", "127.0.0.1")
    proxy_port = int(os.environ.get("PROXY_PORT", "7890"))
    target_port = int(os.environ.get("TARGET_PORT", "7844"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    for listen_ip in listen_ips:
        threading.Thread(
            target=serve_listener,
            args=(listen_ip, listen_port, proxy_host, proxy_port, target_port),
            daemon=True,
        ).start()

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
