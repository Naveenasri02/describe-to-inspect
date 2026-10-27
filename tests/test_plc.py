import socket
import struct
import time

from d2i.plc import Plc
from d2i.station import Verdict


def _ask(port, pdu):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
        head = s.recv(7)
        length = struct.unpack(">H", head[4:6])[0]
        return s.recv(length - 1)


def _coils(port, n=3):
    r = _ask(port, struct.pack(">BHH", 1, 0, n))
    return [bool(r[2] >> i & 1) for i in range(n)]


def _regs(port, n=4):
    r = _ask(port, struct.pack(">BHH", 3, 0, n))
    return list(struct.unpack(">" + "H" * n, r[2:2 + r[1]]))


def test_modbus_read_after_reject():
    plc = Plc(host="127.0.0.1", port=0)
    plc.start()
    try:
        plc.set_verdict(Verdict("REJECT", [{"rule": 2, "check": "x", "pocket": 1, "text": "t"}], []))
        assert _coils(plc.port) == [True, False, False]
        assert _regs(plc.port) == [0, 1, 0, 2]
        assert _ask(plc.port, struct.pack(">BHH", 6, 0, 1)) == bytes([0x86, 1])
        time.sleep(0.6)
        assert _coils(plc.port) == [False, False, False]
        plc.set_verdict(Verdict("HOLD", [], []))
        assert _coils(plc.port) == [False, False, True]
        plc.set_verdict(Verdict("LOCKED", [], []))
        assert _coils(plc.port) == [False, True, False]
        assert _regs(plc.port) == [0, 1, 1, 2]
    finally:
        plc.stop()


def test_stop_closes_open_connections():
    plc = Plc(host="127.0.0.1", port=0)
    plc.start()
    s = socket.create_connection(("127.0.0.1", plc.port), timeout=1)
    try:
        s.sendall(struct.pack(">HHHB", 1, 0, 6, 1) + struct.pack(">BHH", 1, 0, 3))
        assert len(s.recv(64)) > 0
        plc.stop()
        try:
            s.sendall(struct.pack(">HHHB", 2, 0, 6, 1) + struct.pack(">BHH", 1, 0, 3))
            assert s.recv(64) == b""
        except OSError:
            pass
    finally:
        s.close()


def test_supervisor_hold_survives_verdicts():
    plc = Plc(host="127.0.0.1", port=0)
    plc.set_hold(True)
    plc.set_verdict(Verdict("PASS", [], []))
    assert plc.coils[2] is True
    plc.set_hold(False)
    assert plc.coils[2] is False
    plc.set_verdict(Verdict("HOLD", [], []))
    assert plc.coils[2] is True
