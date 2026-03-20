#!/bin/bash
rm -f /config/chrome-profile/SingletonCookie /config/chrome-profile/SingletonLock /config/chrome-profile/SingletonSocket

python3 - <<'PY' &
import socket
import threading

LISTEN_HOST = '0.0.0.0'
LISTEN_PORT = 9223
TARGET_HOST = '127.0.0.1'
TARGET_PORT = 9222


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass
        try:
            src.close()
        except Exception:
            pass


server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((LISTEN_HOST, LISTEN_PORT))
server.listen(50)

while True:
    client, _ = server.accept()
    try:
        target = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=10)
    except Exception:
        client.close()
        continue
    threading.Thread(target=pipe, args=(client, target), daemon=True).start()
    threading.Thread(target=pipe, args=(target, client), daemon=True).start()
PY

wrapped-chromium ${CHROME_CLI}
