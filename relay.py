#!/usr/bin/env python3
"""HotSpell relay server — forwards bytes between two players.

Deploy to Railway (free tier) so both players can connect OUT to this server
without either needing to forward a port on their router.

=== Deploy steps ===
1.  Create a free account at https://railway.app
2.  New Project → Deploy from GitHub repo  (or drag-drop this file)
3.  In the service Settings → Networking → Add TCP Proxy
4.  Railway shows you  e.g.  roundhouse.proxy.rlwy.net : 12345
5.  Set  RELAY_HOST = 'roundhouse.proxy.rlwy.net'
        RELAY_PORT = 12345
    in hot-spell.py, then rebuild the exe.

=== Protocol ===
HOST → SERVER :  "HOST\\n"
SERVER → HOST :  "CODE:ABCDEF\\n"   (6 uppercase hex chars)
(guest connects)
SERVER → HOST :  "GO\\n"
HOST ↔ GUEST  :  transparent game protocol (JSON lines)

GUEST → SERVER :  "JOIN:ABCDEF\\n"
SERVER → GUEST :  "GO\\n"        (host found)
              or  "NOTFOUND\\n"  (bad / expired code)
GUEST ↔ HOST  :  transparent game protocol

After GO the relay is a transparent byte pipe — it knows nothing about the
Hot Spell protocol and requires no changes when the game protocol changes.
"""

import os
import socket
import threading
import secrets
import time

PORT   = int(os.environ.get('PORT', 45680))
_rooms: dict = {}   # code → {'host': conn, 'event': Event, 'guest': conn|None, 'born': float}
_lock         = threading.Lock()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _readline(conn: socket.socket, timeout: float = 30.0) -> str:
    """Read bytes up to and including the first newline within *timeout* s."""
    conn.settimeout(timeout)
    buf = b''
    while len(buf) < 64:
        try:
            b = conn.recv(1)
        except OSError:
            return ''
        if not b:
            return ''
        buf += b
        if b == b'\n':
            return buf.decode('ascii', errors='replace').strip()
    return ''


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    """Forward bytes from *src* → *dst* until a side closes or errors."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


# ── Connection handler ────────────────────────────────────────────────────────

def _handle(conn: socket.socket) -> None:
    try:
        line = _readline(conn, timeout=30)
        if not line:
            conn.close()
            return

        # ── HOST side ────────────────────────────────────────────────────────
        if line == 'HOST':
            code = secrets.token_hex(3).upper()
            with _lock:
                while code in _rooms:
                    code = secrets.token_hex(3).upper()
                event = threading.Event()
                room  = {
                    'host':  conn,
                    'event': event,
                    'guest': None,
                    'born':  time.monotonic(),
                }
                _rooms[code] = room

            conn.sendall(f'CODE:{code}\n'.encode())

            # Wait up to 10 minutes for a guest to join
            if not event.wait(timeout=600):
                with _lock:
                    _rooms.pop(code, None)
                conn.close()
                return

            guest = room['guest']
            conn.sendall(b'GO\n')
            conn.settimeout(None)

            # Bidirectional pipe: spawn guest→host thread, run host→guest here
            t = threading.Thread(target=_pipe, args=(guest, conn), daemon=True)
            t.start()
            _pipe(conn, guest)
            t.join()

            with _lock:
                _rooms.pop(code, None)

        # ── GUEST side ───────────────────────────────────────────────────────
        elif line.startswith('JOIN:'):
            code = line[5:].strip().upper()
            with _lock:
                room = _rooms.get(code)
                if room is None or room.get('guest') is not None:
                    conn.sendall(b'NOTFOUND\n')
                    conn.close()
                    return
                room['guest'] = conn   # register under the lock

            room['event'].set()
            conn.sendall(b'GO\n')
            conn.settimeout(None)
            # HOST thread owns the bidirectional piping; this thread is done.

        else:
            conn.close()

    except Exception:
        try:
            conn.close()
        except Exception:
            pass


# ── Stale-room cleanup ────────────────────────────────────────────────────────

def _cleanup_loop() -> None:
    """Evict rooms where the host waited > 11 minutes without a guest."""
    while True:
        time.sleep(60)
        cutoff = time.monotonic() - 660
        with _lock:
            stale = [
                c for c, r in _rooms.items()
                if r.get('born', 0) < cutoff and r.get('guest') is None
            ]
            for c in stale:
                _rooms.pop(c, None)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', PORT))
    srv.listen(100)
    print(f'HotSpell relay listening on port {PORT}', flush=True)
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    while True:
        try:
            c, _ = srv.accept()
            threading.Thread(target=_handle, args=(c,), daemon=True).start()
        except OSError:
            break


if __name__ == '__main__':
    main()
