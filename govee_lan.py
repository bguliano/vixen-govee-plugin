"""Govee LAN protocol: discovery, status, colour and segment (razer) packets, and the UDP endpoint they share.

Govee devices with LAN Control on listen for JSON on UDP 4003 and answer on 4002. Discovery is a multicast "scan" to
239.255.255.250:4001. Whole-device colour is "colorwc"; per-segment colour uses the undocumented "razer" channel
(the Govee app's DreamView stream): arm it with opcode 0xB1, wait about a second, then stream 0xB0 frames.
"""
import base64
import json
import socket
import threading
import time

MULTICAST_ADDR = "239.255.255.250"
SCAN_PORT = 4001
LISTEN_PORT = 4002
COMMAND_PORT = 4003

RAZER_ARM_DELAY = 1.0  # the firmware drops 0xB0 frames that arrive right after arming


def normalise_device_id(text):
    """Govee device ids are eight colon-separated bytes ("1A:2B:3C:4D:5E:6F:70:81"). Accepts any case or separator."""
    hexdigits = "".join(c for c in str(text) if c in "0123456789abcdefABCDEF")
    if len(hexdigits) != 16:
        return None
    hexdigits = hexdigits.upper()
    return ":".join(hexdigits[i:i + 2] for i in range(0, 16, 2))


def message(cmd, data):
    return json.dumps({"msg": {"cmd": cmd, "data": data}}, separators=(",", ":")).encode()


def scan_message():
    return message("scan", {"account_topic": "reserve"})


def status_message():
    return message("devStatus", {})


def turn_message(on):
    return message("turn", {"value": 1 if on else 0})


def brightness_message(percent):
    return message("brightness", {"value": max(1, min(100, int(percent)))})


def color_message(r, g, b):
    return message("colorwc", {"color": {"r": int(r), "g": int(g), "b": int(b)}, "colorTemInKelvin": 0})


def kelvin_message(kelvin):
    return message("colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": int(kelvin)})


def razer_frame(opcode, payload):
    """BB <len_hi> <len_lo> <opcode> <payload> <XOR of every preceding byte>; len counts the payload only."""
    body = bytes([0xBB, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF, opcode]) + bytes(payload)
    checksum = 0
    for byte in body:
        checksum ^= byte
    return body + bytes([checksum])


def razer_arm_message(on=True):
    return message("razer", {"pt": base64.b64encode(razer_frame(0xB1, [1 if on else 0])).decode()})


def razer_colors_message(colors, gradient=False):
    """colors: [(r, g, b), ...], one per segment, at most 255."""
    colors = list(colors)[:255]
    payload = [1 if gradient else 0, len(colors)]
    for r, g, b in colors:
        payload += [int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF]
    return message("razer", {"pt": base64.b64encode(razer_frame(0xB0, payload)).decode()})


def parse(datagram):
    """(cmd, data) of a device's answer, or None for anything else that lands on port 4002."""
    try:
        inner = json.loads(datagram)["msg"]
        return inner["cmd"], inner.get("data") or {}
    except (ValueError, KeyError, TypeError):
        return None


class Endpoint:
    """One UDP socket bound to 4002 for every controller in the process: sends commands, collects answers.

    Answers are kept per source address (devStatus) and as a list (scan), so callers can wait for what they asked.
    """

    def __init__(self, listen_port=LISTEN_PORT, command_port=COMMAND_PORT, scan_target=(MULTICAST_ADDR, SCAN_PORT)):
        self.command_port = command_port
        self.scan_target = scan_target
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        self.sock.bind(("", listen_port))
        self.sock.settimeout(0.25)
        self._lock = threading.Condition()
        self._scans = []  # [(time, data)]
        self._statuses = {}  # ip -> (time, data)
        self._closed = False
        self.packets_sent = 0
        self.bytes_sent = 0
        self.errors = 0
        self._reader = threading.Thread(target=self._read, name="govee-lan-reader", daemon=True)
        self._reader.start()

    def close(self):
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, ip, payload):
        try:
            self.sock.sendto(payload, (ip, self.command_port))
        except OSError:
            self.errors += 1
            raise
        self.packets_sent += 1
        self.bytes_sent += len(payload)

    def _read(self):
        while not self._closed:
            try:
                datagram, (ip, _port) = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._closed:
                    return
                time.sleep(0.1)
                continue
            parsed = parse(datagram)
            if not parsed:
                continue
            cmd, data = parsed
            now = time.monotonic()
            with self._lock:
                if cmd == "scan":
                    self._scans.append((now, data))
                    del self._scans[:-256]
                elif cmd == "devStatus":
                    self._statuses[ip] = (now, data)
                self._lock.notify_all()

    def scan(self, timeout=2.0, repeats=3):
        """Multicast discovery. Returns {device_id: {"ip", "sku", "device", ...}} for every device that answered."""
        started = time.monotonic()
        for i in range(repeats):
            try:
                self.sock.sendto(scan_message(), self.scan_target)
            except OSError:
                self.errors += 1
            if i + 1 < repeats:
                time.sleep(0.15)
        deadline = started + timeout
        with self._lock:
            while time.monotonic() < deadline:
                self._lock.wait(max(0.0, deadline - time.monotonic()))
            found = {}
            for at, data in self._scans:
                if at >= started:
                    device_id = normalise_device_id(data.get("device", ""))
                    if device_id and data.get("ip"):
                        found[device_id] = dict(data, device=device_id)
            return found

    def status(self, ip, timeout=1.0):
        """devStatus of one device: {"onOff", "brightness", "color", "colorTemInKelvin"}, or None without an answer."""
        asked = time.monotonic()
        try:
            self.send(ip, status_message())
        except OSError:
            return None
        deadline = asked + timeout
        with self._lock:
            while True:
                entry = self._statuses.get(ip)
                if entry and entry[0] >= asked:
                    return entry[1]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._lock.wait(remaining)


_shared = None
_shared_lock = threading.Lock()


def shared_endpoint():
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Endpoint()
        return _shared
