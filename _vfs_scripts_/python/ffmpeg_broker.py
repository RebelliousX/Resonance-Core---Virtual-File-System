#!/usr/bin/env python3
"""Persistent FFmpeg broker.

The broker keeps one coordinator process alive and gives each request its own
FFmpeg subprocess. This guarantees that the FFmpeg executable is launched and
its DLLs are loaded before playback requests, while allowing independent video
and audio requests to run concurrently.
"""
import json
import os
import queue
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time

_PROTOCOL = 1
_MAX_MESSAGE = 16 * 1024 * 1024
_HEADER = struct.Struct("!I")


def _send(sock, value):
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_MESSAGE:
        raise ValueError("broker message too large")
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def _recv(sock):
    header = _read_exact(sock, _HEADER.size)
    if not header:
        return None
    (length,) = _HEADER.unpack(header)
    if length <= 0 or length > _MAX_MESSAGE:
        raise ValueError("invalid broker message length")
    payload = _read_exact(sock, length)
    if payload is None:
        return None
    return json.loads(payload.decode("utf-8"))


def _read_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            return None
        result.extend(chunk)
    return bytes(result)


def _worker_main(ffmpeg):
    token = secrets.token_hex(16)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    print(json.dumps({"port": port, "token": token}), flush=True)
    conn, _ = sock.accept()
    sock.close()
    active = {}
    lock = threading.Lock()
    send_lock = threading.Lock()
    stopping = threading.Event()

    def emit(event):
        with send_lock:
            _send(conn, event)

    def run_request(req):
        rid = req.get("id")
        kind = req.get("kind")
        path = req.get("path")
        if not isinstance(rid, str) or not isinstance(path, str):
            emit({"event": "error", "id": rid, "message": "invalid request"})
            return
        if not os.path.isfile(path):
            emit({"event": "error", "id": rid, "message": "file not found"})
            return
        if kind == "video":
            cmd = [ffmpeg, "-v", "error", "-i", path, "-map", "0:v:0",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        elif kind == "audio":
            rate = int(req.get("rate", 22050))
            channels = int(req.get("channels", 2))
            cmd = [ffmpeg, "-v", "error", "-i", path, "-map", "0:a:0",
                   "-f", "s16le", "-ac", str(channels), "-ar", str(rate), "-"]
        else:
            emit({"event": "error", "id": rid, "message": "unknown request kind"})
            return
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with lock:
                active[rid] = proc
            emit({"event": "started", "id": rid, "kind": kind})
            while True:
                data = proc.stdout.read(64 * 1024)
                if not data:
                    break
                emit({"event": "data", "id": rid, "data": data.hex()})
            code = proc.wait()
            emit({"event": "ended", "id": rid, "code": code})
        except Exception as exc:
            emit({"event": "error", "id": rid, "message": str(exc)})
        finally:
            with lock:
                active.pop(rid, None)

    try:
        emit({"event": "ready", "protocol": _PROTOCOL, "token": token})
        while not stopping.is_set():
            req = _recv(conn)
            if req is None:
                break
            op = req.get("op") if isinstance(req, dict) else None
            if op == "play":
                threading.Thread(target=run_request, args=(req,),
                                 name="ffmpeg-request", daemon=True).start()
            elif op == "stop":
                rid = req.get("id")
                with lock:
                    proc = active.get(rid)
                if proc is not None:
                    proc.terminate()
            elif op == "ping":
                emit({"event": "pong", "id": req.get("id")})
            elif op == "shutdown":
                stopping.set()
                with lock:
                    procs = list(active.values())
                for proc in procs:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                break
    finally:
        with lock:
            procs = list(active.values())
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass


class BrokerStream:
    """One concurrent request returned by :class:`FFmpegBrokerClient`."""
    def __init__(self, client, request_id, kind):
        self.client = client
        self.request_id = request_id
        self.kind = kind
        self._queue = queue.Queue(maxsize=8)
        self._ended = False

    def read(self, timeout=0.5):
        if self._ended and self._queue.empty():
            return b""
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return b""
        if item is None:
            self._ended = True
            return b""
        return item

    def stop(self):
        self.client.stop(self.request_id)

    def _push(self, data):
        self._queue.put(data)

    def _end(self):
        self._ended = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass


class FFmpegBrokerClient:
    """Starts one broker and multiplexes any number of independent streams."""
    def __init__(self, ffmpeg, logger=None):
        self.ffmpeg = os.path.abspath(ffmpeg)
        self.logger = logger
        self.process = None
        self.sock = None
        self._streams = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._reader = None

    def start(self, timeout=5.0):
        if self.process is not None and self.process.poll() is None:
            return True
        script = os.path.abspath(__file__)
        self.process = subprocess.Popen(
            [sys.executable, script, "--worker", self.ffmpeg],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + timeout
        line = b""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False
            if self.process.stdout.readable():
                # The worker prints exactly one small bootstrap line.
                import select
                ready, _, _ = select.select([self.process.stdout], [], [], 0.05)
                if ready:
                    line = self.process.stdout.readline()
                    break
        if not line:
            self.close()
            return False
        info = json.loads(line.decode("utf-8"))
        self.sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=timeout)
        self.sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop,
                                        name="ffmpeg-broker-reader", daemon=True)
        self._reader.start()
        return True

    def play(self, path, kind, rate=22050, channels=2):
        if self.sock is None:
            raise RuntimeError("FFmpeg broker is not running")
        rid = secrets.token_hex(12)
        stream = BrokerStream(self, rid, kind)
        with self._lock:
            self._streams[rid] = stream
        self._send({"op": "play", "id": rid, "kind": kind, "path": os.path.abspath(path),
                    "rate": rate, "channels": channels})
        return stream

    def stop(self, request_id):
        if self.sock is not None:
            self._send({"op": "stop", "id": request_id})

    def _send(self, value):
        with self._send_lock:
            _send(self.sock, value)

    def _read_loop(self):
        try:
            while True:
                event = _recv(self.sock)
                if event is None:
                    break
                stream = self._streams.get(event.get("id"))
                if stream is None:
                    continue
                if event.get("event") == "data":
                    stream._push(bytes.fromhex(event["data"]))
                elif event.get("event") in ("ended", "error"):
                    stream._end()
                    with self._lock:
                        self._streams.pop(stream.request_id, None)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            with self._lock:
                streams = list(self._streams.values())
                self._streams.clear()
            for stream in streams:
                stream._end()

    def close(self):
        if self.sock is not None:
            try:
                self._send({"op": "shutdown"})
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.process.kill()
                except OSError:
                    pass
            self.process = None


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        _worker_main(sys.argv[2])
        return
    raise SystemExit("worker-only module")


if __name__ == "__main__":
    main()
