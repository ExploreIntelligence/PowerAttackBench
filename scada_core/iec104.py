from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from threading import Thread
from typing import Any, Callable, Dict, Optional


@dataclass
class IEC104Message:
    type_name: str
    payload: Dict[str, Any]

    @classmethod
    def from_bytes(cls, raw: bytes) -> "IEC104Message":
        if len(raw) < 4:
            raise ValueError("Incomplete IEC 104 frame")
        length = struct.unpack("!I", raw[:4])[0]
        if len(raw) - 4 < length:
            raise ValueError("Frame truncated")
        body = raw[4:4 + length]
        data = json.loads(body.decode("utf-8"))
        return cls(type_name=data.get("type", "command"), payload=data.get("payload", {}))

    def to_bytes(self) -> bytes:
        body = json.dumps({"type": self.type_name, "payload": self.payload}, separators=(",", ":")).encode("utf-8")
        return struct.pack("!I", len(body)) + body


class IEC104Socket:
    def __init__(self, host: str = "0.0.0.0", port: int = 2404):
        self.host = host
        self.port = port

    def send(self, target_host: str, target_port: int, message: IEC104Message) -> Dict[str, Any]:
        with socket.create_connection((target_host, target_port), timeout=3) as sock:
            sock.sendall(message.to_bytes())
            data = self._recv_all(sock)
            reply = IEC104Message.from_bytes(data)
            return reply.payload

    def _recv_all(self, sock: socket.socket, chunk_size: int = 4096) -> bytes:
        header = sock.recv(4)
        if len(header) < 4:
            raise ConnectionError("No response received")
        length = struct.unpack("!I", header)[0]
        chunks = [header]
        received = 4
        while received < length + 4:
            chunk = sock.recv(min(chunk_size, length + 4 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)


class IEC104Server:
    def __init__(self, host: str, port: int, handler: Callable[[IEC104Message], Dict[str, Any]]):
        self.host = host
        self.port = port
        self.handler = handler
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(10)
        self._thread = Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def _serve(self):
        while True:
            conn, _ = self._server.accept()
            with conn:
                try:
                    raw = self._recv_all(conn)
                    request = IEC104Message.from_bytes(raw)
                    response = self.handler(request)
                    conn.sendall(IEC104Message("response", response).to_bytes())
                except Exception as exc:  # pragma: no cover - defensive handler
                    conn.sendall(IEC104Message("error", {"message": str(exc)}).to_bytes())

    def _recv_all(self, sock: socket.socket, chunk_size: int = 4096) -> bytes:
        header = sock.recv(4)
        if len(header) < 4:
            raise ConnectionError("No request received")
        length = struct.unpack("!I", header)[0]
        chunks = [header]
        received = 4
        while received < length + 4:
            chunk = sock.recv(min(chunk_size, length + 4 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)


class IEC104Client:
    def __init__(self, host: str = "127.0.0.1", port: int = 2404):
        self.host = host
        self.port = port

    def send(self, message: IEC104Message) -> Dict[str, Any]:
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            sock.sendall(message.to_bytes())
            header = sock.recv(4)
            if len(header) < 4:
                raise ConnectionError("No data returned")
            length = struct.unpack("!I", header)[0]
            chunks = [header]
            received = 0
            while received < length:
                chunk = sock.recv(min(4096, length - received))
                if not chunk:
                    raise ConnectionError("Truncated response")
                chunks.append(chunk)
                received += len(chunk)
            raw = b"".join(chunks)
            reply = IEC104Message.from_bytes(raw)
            return reply.payload
