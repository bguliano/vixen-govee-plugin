"""Tests for Govee Output: packets, layout and settings, the controller's worker against a fake LAN, the real UDP
endpoint against fake devices on 127.0.0.1, and the Home Assistant client against a fake server.

Run: python3 -m unittest discover -s tests -v
"""
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import govee_lan as lan  # noqa: E402
import home_assistant as ha  # noqa: E402
import main  # noqa: E402
import vixen_plugin as vp  # noqa: E402

LAMP = "AA:BB:CC:DD:EE:FF:00:11"
STRIP = "11:22:33:44:55:66:77:88"


def decode(payload):
    return json.loads(payload)["msg"]


def razer_bytes(payload):
    return base64.b64decode(decode(payload)["data"]["pt"])


class Packets(unittest.TestCase):
    def test_arm_matches_the_known_frame(self):
        self.assertEqual(razer_bytes(lan.razer_arm_message(True)).hex(), "bb0001b1010a")
        self.assertEqual(razer_bytes(lan.razer_arm_message(False)).hex(), "bb0001b1000b")

    def test_segment_frame_length_and_checksum(self):
        raw = razer_bytes(lan.razer_colors_message([(255, 0, 0), (0, 255, 0), (0, 0, 255)]))
        self.assertEqual(raw[:4], bytes([0xBB, 0x00, 2 + 9, 0xB0]))
        self.assertEqual(raw[4:6], bytes([0, 3]))
        self.assertEqual(raw[6:15], bytes([255, 0, 0, 0, 255, 0, 0, 0, 255]))
        checksum = 0
        for b in raw[:-1]:
            checksum ^= b
        self.assertEqual(raw[-1], checksum)
        self.assertEqual(razer_bytes(lan.razer_colors_message([(1, 2, 3)], gradient=True))[4], 1)

    def test_colour_and_commands(self):
        self.assertEqual(decode(lan.color_message(1, 2, 3)),
                         {"cmd": "colorwc", "data": {"color": {"r": 1, "g": 2, "b": 3}, "colorTemInKelvin": 0}})
        self.assertEqual(decode(lan.brightness_message(0))["data"]["value"], 1)
        self.assertEqual(decode(lan.turn_message(False))["data"]["value"], 0)

    def test_device_ids_normalise(self):
        self.assertEqual(lan.normalise_device_id("aabbccddeeff0011"), LAMP)
        self.assertEqual(lan.normalise_device_id("aa-bb-cc-dd-ee-ff-00-11"), LAMP)
        self.assertIsNone(lan.normalise_device_id("AA:BB"))


class Recorder:
    """Stands in for Studio: collects what the plugin sends."""

    def __init__(self):
        self.messages = []
        self.storage = {}

    def install(self, plugin):
        plugin.send = lambda message: self.messages.append(message)
        plugin.storage_get = lambda key, default=None: self.storage.get(key, default)
        plugin.storage_set = lambda key, value: self.storage.__setitem__(key, value)
        plugin.data_dir = tempfile.mkdtemp()

    def of(self, method):
        return [m["params"] for m in self.messages if m.get("method") == method]


class FakeLan:
    """The shared endpoint, with devices that answer scan and devStatus and remember every command."""

    def __init__(self, devices):
        self.devices = devices  # device id -> {"ip", "sku", "status"}
        self.sent = []  # (ip, msg)

    def scan(self, timeout=2.0, repeats=3):
        return {d: {"ip": v["ip"], "sku": v["sku"], "device": d} for d, v in self.devices.items()}

    def status(self, ip, timeout=1.0):
        return next((v["status"] for v in self.devices.values() if v["ip"] == ip), None)

    def send(self, ip, payload):
        self.sent.append((ip, decode(payload)))

    def to(self, ip, cmd=None):
        return [m for i, m in self.sent if i == ip and (cmd is None or m["cmd"] == cmd)]


def settings(devices, **extra):
    config = {"devices": devices}
    config.update(extra)
    schema = main.plugin._schema(main.plugin.manifest["contributes"]["controllers"][0]["typeId"])
    config = vp.fill_defaults(schema, config)
    errors = vp.validate(schema, config)
    assert not errors, errors
    return config


class Controller(unittest.TestCase):
    def setUp(self):
        self.studio = Recorder()
        self.studio.install(main.plugin)
        self.lan = FakeLan({
            LAMP: {"ip": "10.0.0.5", "sku": "H6008",
                   "status": {"onOff": 1, "brightness": 40, "color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 3000}},
            STRIP: {"ip": "10.0.0.6", "sku": "H61E6",
                    "status": {"onOff": 0, "brightness": 100, "color": {"r": 9, "g": 8, "b": 7}, "colorTemInKelvin": 0}},
        })
        self._shared = lan.shared_endpoint
        lan.shared_endpoint = lambda: self.lan
        self._delay = lan.RAZER_ARM_DELAY
        lan.RAZER_ARM_DELAY = 0.1

    def tearDown(self):
        lan.shared_endpoint = self._shared
        lan.RAZER_ARM_DELAY = self._delay

    def make(self, config, outputs):
        c = main.GoveeController(main.plugin, "inst-1", main.plugin._types[0][0], outputs)
        c.config = main.plugin._configure(c, config)
        c.running = True
        return c

    def test_refuses_too_few_outputs_and_bad_ids(self):
        c = main.GoveeController(main.plugin, "inst-2", main.plugin._types[0][0], 3)
        with self.assertRaises(vp.ConfigError) as caught:
            main.plugin._configure(c, settings([{"deviceId": LAMP}, {"deviceId": STRIP}]))
        self.assertIn("need 6 outputs", str(caught.exception))
        with self.assertRaises(vp.ConfigError) as caught:
            main.plugin._configure(c, settings([{"deviceId": "not-a-govee-device-id"}]))
        self.assertEqual(caught.exception.errors[0]["field"], "/devices/0/deviceId")
        with self.assertRaises(vp.ConfigError):
            main.plugin._configure(c, settings([{"deviceId": LAMP}, {"deviceId": LAMP.lower()}]))

    def test_new_settings_and_old_rows(self):
        c = self.make(settings([{"deviceId": STRIP, "mode": "segmentsReversed", "segments": 3},
                                {"deviceId": LAMP, "ip": "10.9.9.9", "haEntity": "light.x", "reverse": False}],
                               fixedAddresses=[STRIP.lower() + " 10.0.0.66"]), 12)
        strip, lamp = c.devices
        self.assertEqual((strip.mode, strip.reverse, strip.segments, strip.configured_ip), ("segments", True, 3, "10.0.0.66"))
        self.assertEqual((lamp.configured_ip, lamp.entity), ("10.9.9.9", "light.x"), "0.1.0 rows still work")
        bad = main.GoveeController(main.plugin, "inst-3", main.plugin._types[0][0], 3)
        with self.assertRaises(vp.ConfigError) as caught:
            main.plugin._configure(bad, settings([{"deviceId": LAMP}], fixedAddresses=[LAMP + " 300.1.1.1"]))
        self.assertEqual(caught.exception.errors[0]["field"], "/fixedAddresses/0")

    def test_layout_and_output_names(self):
        c = self.make(settings([{"name": "Lamp", "deviceId": LAMP},
                                {"name": "Strip", "deviceId": STRIP, "mode": "segments", "segments": 2}]), 9)
        self.assertEqual([(d.first_output, d.output_count) for d in c.devices], [(0, 3), (3, 6)])
        self.assertEqual(c.output_names()[:4], ["Lamp R", "Lamp G", "Lamp B", "Strip S1 R"])

    def test_show_drives_lamp_and_strip_then_restores(self):
        c = self.make(settings([{"name": "Lamp", "deviceId": LAMP},
                                {"name": "Strip", "deviceId": STRIP, "mode": "segments", "segments": 2, "reverse": True}],
                               keepAliveMs=0, maxFps=40), 9)
        c.start()
        deadline = time.time() + 3
        while time.time() < deadline and not any(s["state"] == "running" for s in self.studio.of("controller.status")):
            time.sleep(0.02)
        c.frame(vp.Frame({"encoding": "u8", "values": base64.b64encode(bytes([10, 20, 30, 1, 2, 3, 4, 5, 6])).decode()}))
        time.sleep(0.4)
        c.running = False
        c.stop()

        lamp = self.lan.to("10.0.0.5")
        self.assertNotIn("turn", [m["cmd"] for m in lamp[:2]], "the lamp was already on")
        self.assertIn({"cmd": "brightness", "data": {"value": 100}}, lamp)
        self.assertIn({"color": {"r": 10, "g": 20, "b": 30}, "colorTemInKelvin": 0},
                      [m["data"] for m in self.lan.to("10.0.0.5", "colorwc")])
        # restored: its colour temperature and brightness from before the show
        self.assertEqual(lamp[-2], {"cmd": "colorwc", "data": {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 3000}})
        self.assertEqual(lamp[-1], {"cmd": "brightness", "data": {"value": 40}})

        strip = self.lan.to("10.0.0.6")
        self.assertEqual(strip[0], {"cmd": "turn", "data": {"value": 1}}, "the strip was off, so it's switched on")
        razer = [base64.b64decode(m["data"]["pt"]) for m in self.lan.to("10.0.0.6", "razer")]
        self.assertEqual(razer[0].hex(), "bb0001b1010a")
        frames = [r for r in razer if r[3] == 0xB0]
        self.assertTrue(frames, "segment frames were streamed after arming")
        self.assertEqual(frames[0][6:12], bytes([4, 5, 6, 1, 2, 3]), "segments reversed")
        self.assertEqual(razer[-1].hex(), "bb0001b1000b", "disarmed at stop")
        self.assertEqual(strip[-1], {"cmd": "turn", "data": {"value": 0}}, "switched back off")
        self.assertEqual(self.studio.storage["addresses"], {LAMP: "10.0.0.5", STRIP: "10.0.0.6"})

    def test_rate_limit_and_keepalive(self):
        c = self.make(settings([{"deviceId": LAMP}], maxFps=5, keepAliveMs=200, onStop="leave"), 3)
        c.start()
        time.sleep(0.2)
        for level in range(50):
            c.frame(vp.Frame({"encoding": "u8", "values": base64.b64encode(bytes([level, 0, 0])).decode()}))
            time.sleep(0.01)
        time.sleep(0.6)
        c.running = False
        c.stop()
        colours = self.lan.to("10.0.0.5", "colorwc")
        # 0.5 s of changes at 5 per second, the newest value last, then keepalive repeats of it
        self.assertLessEqual(len(colours), 8)
        self.assertEqual(colours[-1]["data"]["color"]["r"], 49)
        self.assertGreaterEqual(sum(1 for m in colours if m["data"]["color"]["r"] == 49), 2)

    def test_missing_device_reports_unavailable(self):
        self.lan.devices.clear()
        c = self.make(settings([{"name": "Lamp", "deviceId": LAMP}]), 3)
        c.start()
        time.sleep(2.3)
        c.running = False
        c.stop()
        self.assertEqual(self.studio.of("controller.status")[-1]["state"], "unavailable")
        self.assertEqual(self.lan.sent, [])


class FakeDevices:
    """Govee devices on 127.0.0.1: answer scan on one port and devStatus on another, replying to the sender's 4002."""

    def __init__(self, reply_port):
        self.reply_port = reply_port
        self.scan_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.scan_sock.bind(("127.0.0.1", 0))
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.bind(("127.0.0.1", 0))
        self.received = []
        for s in (self.scan_sock, self.cmd_sock):
            s.settimeout(0.2)
            threading.Thread(target=self._serve, args=(s,), daemon=True).start()

    def _serve(self, sock):
        while True:
            try:
                data, (ip, _port) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            cmd, _ = lan.parse(data)
            self.received.append(cmd)
            if cmd == "scan":
                answer = {"ip": "127.0.0.1", "device": LAMP.lower(), "sku": "H6008"}
            elif cmd == "devStatus":
                answer = {"onOff": 1, "brightness": 77, "color": {"r": 1, "g": 2, "b": 3}, "colorTemInKelvin": 0}
            else:
                continue
            sock.sendto(lan.message(cmd, answer), (ip, self.reply_port))


class RealEndpoint(unittest.TestCase):
    def test_scan_and_status_over_udp(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        listen_port = probe.getsockname()[1]
        probe.close()
        devices = FakeDevices(listen_port)
        endpoint = lan.Endpoint(listen_port=listen_port, command_port=devices.cmd_sock.getsockname()[1],
                                scan_target=("127.0.0.1", devices.scan_sock.getsockname()[1]))
        try:
            found = endpoint.scan(timeout=0.6)
            self.assertEqual(found[LAMP]["ip"], "127.0.0.1")
            self.assertEqual(endpoint.status("127.0.0.1")["brightness"], 77)
            endpoint.send("127.0.0.1", lan.color_message(1, 2, 3))
            time.sleep(0.1)
            self.assertIn("colorwc", devices.received)
        finally:
            endpoint.close()


class FakeHomeAssistant(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def _answer(self, body, code=200):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer good":
            return self._answer({"message": "nope"}, 401)
        if self.path == "/api/":
            return self._answer({"message": "API running."})
        if self.path == "/api/states":
            return self._answer([
                {"entity_id": "light.bedroom_desk_lamp", "attributes": {"friendly_name": "Desk Lamp"}},
                {"entity_id": "sensor.desk_lamp_status", "attributes": {
                    "friendly_name": "Desk Lamp Status",
                    "platform_metadata": {"device": LAMP, "sku": "H6008", "capabilities": [{"instance": "colorRgb"}]}}},
            ])
        self._answer({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeHomeAssistant.calls.append((self.path, body))
        if self.path.startswith("/api/services/script/light_acquire"):
            return self._answer({"changed_states": [], "service_response": {
                "granted": ["light.bedroom_desk_lamp"], "denied": [], "denied_owners": []}})
        self._answer({"changed_states": [], "service_response": {"released": body["lights"], "denied": []}})


class HomeAssistant(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), FakeHomeAssistant)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_client(self):
        client = ha.Client(self.url, "good")
        self.assertEqual(client.check()["message"], "API running.")
        self.assertEqual(client.govee_devices()[LAMP]["entity"], "light.bedroom_desk_lamp")
        self.assertEqual(client.acquire(["light.bedroom_desk_lamp"], "Script", "show", False)["granted"],
                         ["light.bedroom_desk_lamp"])
        path, body = FakeHomeAssistant.calls[-1]
        self.assertEqual(path, "/api/services/script/light_acquire?return_response")
        self.assertEqual(body["requester"], "Script")
        with self.assertRaises(ha.HomeAssistantError) as caught:
            ha.Client(self.url, "bad").check()
        self.assertIn("401", str(caught.exception))

    def test_show_acquires_and_releases(self):
        studio = Recorder()
        studio.install(main.plugin)
        with open(ha.token_path(main.plugin.data_dir), "w") as f:
            f.write("good\n")
        fake = FakeLan({LAMP: {"ip": "10.0.0.5", "sku": "H6008", "status": {"onOff": 1, "brightness": 100}}})
        shared = lan.shared_endpoint
        lan.shared_endpoint = lambda: fake
        try:
            c = main.GoveeController(main.plugin, "inst-ha", main.plugin._types[0][0], 3)
            # no haEntity: the plugin matches the lamp to its Home Assistant light by Govee id
            c.config = main.plugin._configure(c, settings(
                [{"deviceId": LAMP}], homeAssistant={"enabled": True, "url": self.url}))
            c.running = True
            FakeHomeAssistant.calls.clear()
            c.start()
            time.sleep(2.5)
            c.running = False
            c.stop()
        finally:
            lan.shared_endpoint = shared
        paths = [p for p, _ in FakeHomeAssistant.calls if p.startswith("/api/services")]
        self.assertEqual(paths, ["/api/services/script/light_acquire?return_response",
                                 "/api/services/script/light_release?return_response"])
        self.assertEqual(FakeHomeAssistant.calls[-1][1]["lights"], ["light.bedroom_desk_lamp"])


if __name__ == "__main__":
    unittest.main()
