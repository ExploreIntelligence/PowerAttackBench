#!/usr/bin/env python3
"""
iec104-master.py -- standards-conformant IEC 60870-5-104 master library.

Scope: PUBLIC STANDARD KNOWLEDGE ONLY.
  - IEC 60870-5-104 transport (APCI: I/S/U frames, N(S)/N(R) window control)
  - IEC 60870-5-101 ASDU coding for common monitored/control information
  This module deliberately contains NO target-specific information: no point
  addresses, no common-address partition rules, no system value ranges.
  Those must be discovered by the caller.

Why this library exists / the rules that matter in practice
-----------------------------------------------------------
1. ACKNOWLEDGE WHAT YOU RECEIVE. Every incoming I-frame carries a send
   sequence N(S) and MUST be acknowledged via N(R). A master that only
   piggybacks N(R) on its own outgoing I-frames stops acknowledging during
   pure receive phases (e.g. a general-interrogation burst); once the
   unacknowledged count reaches the slave's window (default k=12) the slave
   first stops sending and, if T1 (default 15 s) expires, CLOSES the TCP
   connection. Rule of thumb: send a standalone S-frame immediately after
   each received I-frame. That is what this library does.
2. ONE PERSISTENT CONNECTION PER CONTROLLER. Opening a new TCP connection
   per probe exhausts the server's connection slots and leaks TIME_WAIT
   sockets; it can even starve other masters sharing the same controllers.
   Connect once, reuse the connection, close it cleanly when done.
3. RESPECT THE SEND WINDOW. Do not fire more than k unacknowledged I-frames.
4. PACES COMMANDS. High command rates may be rejected by policy or treated
   as abnormal traffic; use the min_interval parameter.

Dependencies: Python 3.8+ standard library only (socket/struct/threading/
queue/time). Run read-only self check:
    python3 iec104-master.py <controller-ip> [ca]
"""

import argparse
import queue
import socket
import struct
import threading
import time

# ---------------------------------------------------------------------------
# Constants -- IEC 60870-5-104 (transport) and -5-101 (ASDU)
# ---------------------------------------------------------------------------

START_BYTE = 0x68

# U-frame function bits (control field octet 1)
U_STARTDT_ACT = 0x07
U_STARTDT_CON = 0x0B
U_STOPDT_ACT = 0x04
U_STOPDT_CON = 0x08
U_TESTFR_ACT = 0x43
U_TESTFR_CON = 0x83

# Cause of transmission (COT), low 6 bits; bit 6 is P/N, bit 7 is T
COT_ACT = 6
COT_ACT_CON = 7
COT_ACT_TERM = 10

# Negative-confirmation diagnostic causes used on the ACT_CON of commands
NEG_CAUSE_NAMES = {
    44: "UNKNOWN_TYPE_ID",
    45: "UNKNOWN_COT",
    46: "UNKNOWN_CA",
    47: "UNKNOWN_IOA",
}

# Monitored information object sizes (information-element payload, bytes)
# after the 3-byte IOA.  Only the common types; other types return raw.
MON_SIZE = {
    1: 1,    # M_SP_NA_1  single-point
    3: 1,    # M_DP_NA_1  double-point
    9: 3,    # M_ME_NA_1  normalized measured value (NVA + QDS)
    11: 3,   # M_ME_NB_1 scaled measured value (SVA + QDS)
    13: 5,   # M_ME_NC_1 short floating-point measured value (R32 + QDS)
}

TYPE_NAMES = {
    1: "M_SP_NA_1", 3: "M_DP_NA_1", 9: "M_ME_NA_1", 11: "M_ME_NB_1",
    13: "M_ME_NC_1", 45: "C_SC_NA_1", 46: "C_DC_NA_1", 48: "C_SE_NA_1",
    49: "C_SE_NB_1", 50: "C_SE_NC_1", 100: "C_IC_NA_1",
}


class IEC104Error(Exception):
    """Base class for IEC-104 transport/protocol failures."""


class ConnectionClosed(IEC104Error):
    """The peer closed the connection or it was lost."""


# ---------------------------------------------------------------------------
# Stateless ASDU encode/decode (pure functions, easy to unit test)
# ---------------------------------------------------------------------------

def encode_asdu(tid, ioa, payload, ca=1, cot=COT_ACT, num=1, sq=False):
    """Build one ASDU with a single information object (VSQ number=1)."""
    vsq = (0x80 if sq else 0x00) | (num & 0x7F)
    return (
        bytes([tid, vsq, cot & 0xFF, 0])
        + struct.pack("<H", ca & 0xFFFF)
        + (ioa & 0xFFFFFF).to_bytes(3, "little")
        + bytes(payload)
    )


def decode_asdu(buf):
    """Parse an ASDU. Returns dict; unknown object types keep raw payload."""
    if len(buf) < 6:
        return None
    tid = buf[0]
    sq = bool(buf[1] & 0x80)
    num = buf[1] & 0x7F
    cot_raw = buf[2]
    cot = cot_raw & 0x3F
    pn = (cot_raw >> 6) & 0x01
    test = (cot_raw >> 7) & 0x01
    ca = struct.unpack("<H", buf[4:6])[0]
    body = buf[6:]
    objs = []
    esize = MON_SIZE.get(tid)
    if num > 0 and esize and len(body) >= 3:
        if sq:
            base = body[0] | (body[1] << 8) | (body[2] << 16)
            data = body[3:]
            for k in range(num):
                chunk = data[k * esize:(k + 1) * esize]
                if len(chunk) < esize:
                    break
                objs.append((base + k, chunk))
        else:
            for k in range(num):
                off = k * (3 + esize)
                chunk = body[off:off + 3 + esize]
                if len(chunk) < 3 + esize:
                    break
                ioa = chunk[0] | (chunk[1] << 8) | (chunk[2] << 16)
                objs.append((ioa, chunk[3:]))
    return {
        "tid": tid,
        "type_name": TYPE_NAMES.get(tid),
        "sq": sq,
        "num": num,
        "cot": cot,
        "pn": pn,
        "test": test,
        "ca": ca,
        "ioas": objs,
        "raw": body.hex(),
    }


# ---------------------------------------------------------------------------
# Command payload builders -- standard value/qualifier coding only.
# QOC byte (45/46): value bits + QU in its standard bit positions + SE bit 7.
# QOS byte (48/49/50): QU bits 0..2, SE at bit 7.
# SE=0 means "execute immediately" (no select-before-operate).
# ---------------------------------------------------------------------------

def qoc(value_bits, qu=0, select=False):
    return bytes([(value_bits & 0x3F) | ((qu & 0x07) << 1) | (0x80 if select else 0)])


def qos(qu=0, select=False):
    return bytes([(qu & 0x07) | (0x80 if select else 0)])


def sc_payload(on, qu=0, select=False):
    """C_SC_NA_1 single command: SCS is bit 0 of the QOC byte."""
    return qoc(1 if on else 0, qu=qu, select=select)


def dc_payload(state, qu=0, select=False):
    """C_DC_NA_1 double command: DCS in bits 0..1 (1=OFF, 2=ON per standard)."""
    return qoc(state & 0x03, qu=qu, select=select)


def se_na_payload(value, qu=0, select=False):
    """C_SE_NA_1 normalized set-point: int16 (-32768..32767) + QOS."""
    return struct.pack("<h", max(-32768, min(32767, int(value)))) + qos(qu, select)


def se_nb_payload(value, qu=0, select=False):
    """C_SE_NB_1 scaled set-point: int16 + QOS."""
    return struct.pack("<h", max(-32768, min(32767, int(value)))) + qos(qu, select)


def se_nc_payload(value, qu=0, select=False):
    """C_SE_NC_1 short floating-point set-point: float32 + QOS."""
    return struct.pack("<f", float(value)) + qos(qu, select)


# ---------------------------------------------------------------------------
# Master
# ---------------------------------------------------------------------------

class IEC104Master:
    """
    One persistent, standards-conformant IEC-104 master connection.

    Usage:
        with IEC104Master("<controller-ip>") as m:
            points, term = m.general_interrogation()
            ioa = ...  # information object address: discover it by probing
            verdict = m.issue_command(45, ioa, sc_payload(True))

    Threading model: a background reader thread owns all reads. It sends an
    S-ack for every received I-frame, hands parsed ASDUs to a queue, and
    tracks the peer's N(R) so the sender respects the k window.
    """

    def __init__(self, host, port=2404, ca=1, timeout=5.0,
                 connect_timeout=5.0, k=12, t1=15.0, min_interval=0.05):
        self.host = host
        self.port = port
        self.ca = ca
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.k = k          # max unacknowledged sent I-frames
        self.t1 = t1        # max wait for N(R) before declaring the link dead
        self.min_interval = min_interval

        self._sock = None
        self._send_lock = threading.Lock()
        self._cv = threading.Condition()
        self._asdu_q = queue.Queue()
        self._reader = None
        self._stop = threading.Event()
        self._closed = False

        # sequence numbers (15-bit, modulo 0x8000)
        self._ns = 0                # next send sequence
        self._peer_ack = 0          # peer's latest N(R) (acks OUR frames)
        self._nr = 0                # next receive sequence expected from peer
        self._last_send_ts = 0.0

        # diagnostics counters
        self.iframes_sent = 0
        self.iframes_recv = 0
        self.sframes_sent = 0

    # ---- connection management --------------------------------------------

    def connect(self):
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=self.connect_timeout)
        self._sock.settimeout(self.timeout)
        # STARTDT handshake synchronously, before starting the reader
        self._send_u(U_STARTDT_ACT)
        apdu = self._read_frame_raw()
        if not self._is_u(apdu, U_STARTDT_CON):
            raise IEC104Error("STARTDT not confirmed: %s" % apdu.hex())
        self._stop.clear()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        return self

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        try:
            if self._sock:
                self._send_u(U_STOPDT_ACT)
                time.sleep(0.1)
        except OSError:
            pass
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        if self._reader:
            self._reader.join(timeout=1.0)

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def reconnect(self):
        """Clean close + fresh connect/handshake (resets all sequences)."""
        self.close()
        self._closed = False
        self._stop.clear()
        while True:
            try:
                item = self._asdu_q.get_nowait()
            except queue.Empty:
                break
        self._ns = self._peer_ack = self._nr = 0
        return self.connect()

    # ---- low level framing -------------------------------------------------

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionClosed("peer closed connection")
            data += chunk
        return data

    def _read_frame_raw(self):
        """Read exactly one APDU (used for the STARTDT handshake)."""
        head = self._recv_exact(2)
        if head[0] != START_BYTE:
            raise IEC104Error("frame desync: %s" % head.hex())
        return head + self._recv_exact(head[1])

    @staticmethod
    def _is_u(apdu, fn):
        return len(apdu) >= 6 and apdu[2] == fn

    def _send_raw(self, frame):
        if self._sock is None:
            raise ConnectionClosed("not connected")
        try:
            self._sock.sendall(frame)
        except OSError as exc:
            raise ConnectionClosed(str(exc)) from exc

    def _send_u(self, fn):
        with self._send_lock:
            self._send_raw(bytes([START_BYTE, 4, fn, 0, 0, 0]))

    def _send_s(self):
        frame = bytes([START_BYTE, 4, 1, 0]) + struct.pack(
            "<H", (self._nr & 0x7FFF) << 1)
        with self._send_lock:
            self._send_raw(frame)
            self.sframes_sent += 1

    def _unacked(self):
        return (self._ns - self._peer_ack) & 0x7FFF

    def _send_i(self, asdu):
        # window flow control: wait for N(R) before exceeding k
        deadline = time.time() + self.t1
        with self._cv:
            while self._unacked() >= self.k:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise ConnectionClosed(
                        "send window full: %d unacknowledged I-frames and no "
                        "N(R) within T1=%.0fs" % (self._unacked(), self.t1))
                self._cv.wait(timeout=remaining)
        # rate pacing
        wait = self.min_interval - (time.time() - self._last_send_ts)
        if wait > 0:
            time.sleep(wait)
        frame = bytes([START_BYTE, 4 + len(asdu)]) + struct.pack(
            "<HH", (self._ns & 0x7FFF) << 1, (self._nr & 0x7FFF) << 1) + asdu
        with self._send_lock:
            self._send_raw(frame)
            self._ns = (self._ns + 1) & 0x7FFF
            self.iframes_sent += 1
            self._last_send_ts = time.time()

    # ---- reader thread -----------------------------------------------------

    def _reader_loop(self):
        buf = b""
        while not self._stop.is_set():
            try:
                # fill until at least a header
                while len(buf) < 2:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise ConnectionClosed("peer closed connection")
                    buf += chunk
                if buf[0] != START_BYTE:
                    idx = buf.find(bytes([START_BYTE]), 1)
                    buf = buf[idx:] if idx >= 0 else b""
                    continue
                apdu_len = buf[1]
                while len(buf) < 2 + apdu_len:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise ConnectionClosed("peer closed connection")
                    buf += chunk
                apdu = buf[:2 + apdu_len]
                buf = buf[2 + apdu_len:]
                self._handle_apdu(apdu)
            except (OSError, IEC104Error) as exc:
                if not self._stop.is_set():
                    self._asdu_q.put({"_error": str(exc)})
                break

    def _handle_apdu(self, apdu):
        ctrl = apdu[2:6]
        first = ctrl[0]
        if first & 0x01 == 0x00:
            # I-frame: N(S) in bytes 0..1, N(R) in bytes 2..3
            peer_ns = struct.unpack("<H", ctrl[0:2])[0] >> 1
            peer_nr = struct.unpack("<H", ctrl[2:4])[0] >> 1
            if peer_ns != self._nr:
                # Sequence gap -> per standard the link must be closed.
                raise ConnectionClosed(
                    "N(S) mismatch: expected %d got %d" % (self._nr, peer_ns))
            self._nr = (self._nr + 1) & 0x7FFF
            # piggybacked acknowledgement of our traffic
            with self._cv:
                self._peer_ack = peer_nr
                self._cv.notify_all()
            # acknowledge THIS frame immediately with an S-frame
            self._send_s()
            self.iframes_recv += 1
            parsed = decode_asdu(apdu[6:])
            if parsed is not None:
                self._asdu_q.put(parsed)
        elif first & 0x03 == 0x01:
            # S-frame: N(R) acknowledges our traffic
            peer_nr = struct.unpack("<H", ctrl[2:4])[0] >> 1
            with self._cv:
                self._peer_ack = peer_nr
                self._cv.notify_all()
        else:
            # U-frame
            if first == U_TESTFR_ACT:
                with self._send_lock:
                    self._send_raw(bytes([START_BYTE, 4, U_TESTFR_CON, 0, 0, 0]))
            # TESTFR_CON / STARTDT_CON / STOPDT_CON need no action here

    # ---- receive helpers ---------------------------------------------------

    def _next_parsed(self, timeout):
        try:
            item = self._asdu_q.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, dict) and "_error" in item:
            raise ConnectionClosed(item["_error"])
        return item

    def drain(self, duration=0.5):
        """Discard queued ASDUs for `duration` (call before a fresh action)."""
        end = time.time() + duration
        while time.time() < end:
            try:
                item = self._asdu_q.get(timeout=max(0.0, end - time.time()))
            except queue.Empty:
                break
            if isinstance(item, dict) and "_error" in item:
                raise ConnectionClosed(item["_error"])

    def iter_asdus(self, total_timeout, idle_timeout=None):
        """
        Yield parsed ASDUs until total_timeout elapses (idle-based optional).
        """
        total_end = time.time() + total_timeout
        idle_end = time.time() + (idle_timeout or total_timeout)
        while time.time() < total_end and time.time() < idle_end:
            timeout = min(total_end - time.time(), idle_end - time.time())
            item = self._next_parsed(max(0.01, timeout))
            if item is None:
                break
            idle_end = time.time() + (idle_timeout or total_timeout)
            yield item

    # ---- standard services -------------------------------------------------

    def general_interrogation(self, ca=None, qoi=20, total_timeout=8.0,
                              idle_timeout=1.5):
        """
        Send C_IC_NA_1 (type 100, IOA=0, QOI=20=station-wide).
        Returns (list_of_monitored_asdu_dicts, got_activation_termination).
        """
        ca = self.ca if ca is None else ca
        self.drain(0.2)
        asdu = (
            bytes([100, 1, COT_ACT, 0])
            + struct.pack("<H", ca)
            + b"\x00\x00\x00"          # IOA = 0
            + bytes([qoi])
        )
        self._send_i(asdu)
        monitored = []
        terminated = False
        for p in self.iter_asdus(total_timeout, idle_timeout):
            if p["tid"] == 100 and p["cot"] == COT_ACT_TERM:
                terminated = True
                # ACT_TERM usually arrives last; keep draining briefly
                idle_end = time.time() + 0.3
                for extra in self.iter_asdus(0.3, 0.3):
                    if extra["tid"] != 100:
                        monitored.append(extra)
                break
            if p["tid"] != 100:
                monitored.append(p)
        return monitored, terminated

    def issue_command(self, tid, ioa, payload, ca=None, cot=COT_ACT,
                      wait_term_timeout=5.0):
        """
        Send one command I-frame and classify the standard response.

        Returns a dict:
          {
            "type": tid, "ioa": ioa,
            "positive_con": bool,      # ACT_CON with P/N=0
            "negative": bool,          # ACT_CON with P/N=1 (incl. diag 44-47)
            "con_cot": int|None,       # 7 plain; 44/45/46/47 diagnostic
            "diagnostic": str|None,    # UNKNOWN_TYPE_ID / UNKNOWN_COT / ...
            "pn": int|None,
            "act_term": bool,          # activation termination received
            "responded": bool,         # any confirmation at all arrived
          }
        Matching is by type id and IOA present in the confirmation ASDU.
        """
        ca = self.ca if ca is None else ca
        self.drain(0.2)
        self._send_i(encode_asdu(tid, ioa, payload, ca=ca, cot=cot))

        verdict = {
            "type": tid, "ioa": ioa, "positive_con": False, "negative": False,
            "con_cot": None, "diagnostic": None, "pn": None,
            "act_term": False, "responded": False,
        }
        end = time.time() + wait_term_timeout
        while time.time() < end and not (
                verdict["positive_con"] and verdict["act_term"]):
            remaining = end - time.time()
            p = self._next_parsed(min(0.05, remaining))
            if p is None:
                break
            if p["tid"] != tid:
                continue
            ioa_list = [io for io, _ in p["ioas"]]
            if ioa_list and ioa not in ioa_list:
                continue
            if p["cot"] == COT_ACT_CON and not verdict["responded"]:
                verdict["responded"] = True
                verdict["pn"] = p["pn"]
                verdict["con_cot"] = p["cot"]
                verdict["positive_con"] = (p["pn"] == 0)
            elif p["cot"] in NEG_CAUSE_NAMES:
                # negative ACT_CON carrying a diagnostic cause (P/N=1)
                verdict["responded"] = True
                verdict["negative"] = True
                verdict["positive_con"] = False
                verdict["pn"] = p["pn"]
                verdict["con_cot"] = p["cot"]
                verdict["diagnostic"] = NEG_CAUSE_NAMES[p["cot"]]
            elif p["cot"] == COT_ACT_TERM:
                verdict["act_term"] = True
        if verdict["pn"] == 1 and not verdict["negative"]:
            verdict["negative"] = True
        return verdict


# ---------------------------------------------------------------------------
# Read-only self check: STARTDT + general interrogation, then clean close.
# ---------------------------------------------------------------------------

def _main():
    parser = argparse.ArgumentParser(description="IEC-104 read-only GI self check")
    parser.add_argument("host")
    parser.add_argument("ca", nargs="?", type=int, default=1)
    parser.add_argument("--port", type=int, default=2404)
    parser.add_argument("--listen", type=float, default=8.0)
    args = parser.parse_args()

    with IEC104Master(args.host, port=args.port, ca=args.ca) as master:
        print("[*] STARTDT confirmed, sending general interrogation ...")
        points, terminated = master.general_interrogation(
            total_timeout=args.listen)
        print("[*] ACT_TERM received: %s" % terminated)
        print("[*] ASDUs received: %d, S-acks sent: %d, I-frames sent: %d"
              % (master.iframes_recv, master.sframes_sent,
                 master.iframes_sent))
        total_objs = 0
        for p in points:
            for ioa, data in p["ioas"]:
                total_objs += 1
                val = ""
                if p["tid"] == 11 and len(data) >= 2:
                    val = " scaled=%d" % struct.unpack("<h", data[:2])[0]
                elif p["tid"] == 13 and len(data) >= 4:
                    val = " float=%.4f" % struct.unpack("<f", data[:4])[0]
                print("  tid=%-3d %-9s cot=%-2d ca=%-3d ioa=%-7d%s"
                      % (p["tid"], p["type_name"] or "?", p["cot"],
                         p["ca"], ioa, val))
        print("[*] %d information objects collected; connection stays up: OK"
              % total_objs)


if __name__ == "__main__":
    _main()
