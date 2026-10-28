"""Modbus TCP outputs for the packing machine's PLC (read-only FC1 and FC3)."""
import socket
import socketserver
import struct
import threading
import time

PULSE_S = 0.5


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        plc = self.server.plc
        with plc._lock:
            plc._conns.add(self.request)
        try:
            self._serve(plc)
        finally:
            with plc._lock:
                plc._conns.discard(self.request)

    def _serve(self, plc):
        while True:
            head = self._read(7)
            if head is None:
                return
            tid, pid, length, unit = struct.unpack(">HHHB", head)
            if pid != 0 or length < 2 or length > 254:
                return
            pdu = self._read(length - 1)
            if pdu is None:
                return
            out = plc._answer(pdu)
            try:
                self.request.sendall(struct.pack(">HHHB", tid, 0, len(out) + 1, unit) + out)
            except OSError:
                return

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.request.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Plc:
    def __init__(self, host="0.0.0.0", port=5020):
        self.host, self.port = host, port
        self._lock = threading.Lock()
        self._reject_until = 0.0
        self._locked = self._hold = self._agent_hold = False
        self.regs = [0, 0, 0, 0]
        self._server = None
        self._thread = None
        self._conns = set()

    @property
    def coils(self):
        with self._lock:
            return [time.monotonic() < self._reject_until, self._locked, self._hold or self._agent_hold]

    @property
    def connected(self) -> bool:
        """True while a machine (Modbus client) holds a connection open."""
        with self._lock:
            return bool(self._conns)

    def set_hold(self, on: bool):
        """Shift Supervisor hold on coil 2; verdicts never clear it, only a QA-approved resume does."""
        with self._lock:
            self._agent_hold = on

    def set_locked(self, on: bool):
        """Coil 1 = the station's real lock state (self-test, teach, approval, shift expiry), not only the last verdict."""
        with self._lock:
            self._locked = on

    def set_verdict(self, v):
        with self._lock:
            self._locked = v.status == "LOCKED"
            self._hold = v.status == "HOLD"
            slot = {"PASS": 0, "REJECT": 1, "HOLD": 2}.get(v.status)
            if slot is not None:
                self.regs[slot] = (self.regs[slot] + 1) & 0xFFFF
            if v.status == "REJECT":
                self._reject_until = time.monotonic() + PULSE_S
                rules = [r["rule"] for r in v.reasons if isinstance(r.get("rule"), int)]
                if rules:
                    self.regs[3] = rules[0] & 0xFFFF

    def eject(self):
        """Reject pulse only (a known-bad physical challenge strip): the verdict counters and the other coils stay."""
        with self._lock:
            self._reject_until = time.monotonic() + PULSE_S

    def _answer(self, pdu):
        fc = pdu[0]
        if fc in (1, 3) and len(pdu) == 5:
            start, qty = struct.unpack(">HH", pdu[1:])
            items = self.coils if fc == 1 else list(self.regs)
            if qty < 1 or start + qty > len(items):
                return bytes([fc | 0x80, 2])
            items = items[start:start + qty]
            if fc == 3:
                return bytes([3, qty * 2]) + struct.pack(">" + "H" * qty, *items)
            data = bytearray((qty + 7) // 8)
            for i, on in enumerate(items):
                if on:
                    data[i // 8] |= 1 << (i % 8)
            return bytes([1, len(data)]) + bytes(data)
        return bytes([fc | 0x80, 1])

    def start(self):
        self._server = _Server((self.host, self.port), _Handler)
        self._server.plc = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join()
            with self._lock:
                conns, self._conns = list(self._conns), set()
            for c in conns:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                c.close()
            self._server = None
