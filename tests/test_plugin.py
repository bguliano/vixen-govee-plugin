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


BULBS = ["0A:00:00:00:00:00:00:%02X" % i for i in range(1, 6)]


class OutputCount(unittest.TestCase):
    """0.2.0: Vixen Studio with plugin API 1.1 sizes the controller from outputs_for; older Studio still gets refusals."""

    def setUp(self):
        self.studio = Recorder()
        self.studio.install(main.plugin)
        main.plugin.capabilities = {}

    def tearDown(self):
        main.plugin.capabilities = {}

    def outputs_for(self, config):
        type_id = main.plugin._types[0][0]
        return main.plugin._handle("controller.outputsFor", {"typeId": type_id, "config": config})

    def test_five_bulbs_need_fifteen_outputs_named_per_light(self):
        rows = [{"name": f"Bulb {i + 1}", "deviceId": d} for i, d in enumerate(BULBS)]
        answer = self.outputs_for({"devices": rows})
        self.assertEqual(answer["outputCount"], 15)
        self.assertEqual(answer["outputNames"][:4], ["Bulb 1 R", "Bulb 1 G", "Bulb 1 B", "Bulb 2 R"])

    def test_segments_and_disabled_rows_keep_their_outputs(self):
        rows = [{"name": "Strip", "deviceId": STRIP, "mode": "segments", "segments": 2},
                {"name": "Lamp", "deviceId": LAMP, "enabled": False}]
        answer = self.outputs_for({"devices": rows})
        self.assertEqual(answer["outputNames"], ["Strip S1 R", "Strip S1 G", "Strip S1 B", "Strip S2 R", "Strip S2 G",
                                                 "Strip S2 B", "Lamp R", "Lamp G", "Lamp B"])
        self.assertEqual(self.outputs_for({})["outputCount"], 0)

    def test_the_manifest_declares_it_where_older_studio_ignores_it(self):
        schema = main.plugin.manifest["contributes"]["controllers"][0]
        self.assertNotIn("outputCount", schema)
        self.assertEqual(schema["configSchema"]["x-outputCount"], "fromConfig")
        self.assertEqual(schema["configSchema"]["x-actions"][0]["tool"], "detect_lights")
        self.assertIn("detect_lights", main.plugin._tools)

    def test_oversize_tables_are_refused_only_without_controller_outputs(self):
        c = main.GoveeController(main.plugin, "inst-oc", main.plugin._types[0][0], 3)
        config = settings([{"deviceId": LAMP}, {"deviceId": STRIP}])
        with self.assertRaises(vp.ConfigError):
            main.plugin._configure(c, config)
        main.plugin._handle("initialize", {"capabilities": {"controllerOutputs": True}, "muted": True})
        self.assertTrue(main.plugin.host_sets_output_count)
        self.assertEqual(len(main.plugin._configure(c, config)["devices"]), 2)
        main.controllers.pop("inst-oc", None)


class Detection(unittest.TestCase):
    def test_rows_are_added_and_existing_ones_keep_their_settings(self):
        found = {LAMP: {"ip": "10.0.0.5", "sku": "H6008"}, STRIP: {"ip": "10.0.0.6", "sku": "H61E6"},
                 BULBS[0]: {"ip": "10.0.0.7", "sku": "H6008"}}
        names = {LAMP: {"name": "Desk Lamp", "segments": 0}, STRIP: {"name": "TV Strip", "segments": 12}}
        existing = [{"name": "", "deviceId": LAMP, "mode": "color", "segments": 1, "enabled": False},
                    {"name": "Gone", "deviceId": BULBS[4], "mode": "color", "segments": 1, "enabled": True}]
        rows, added, updated, missing, whites = main.merge_detected(existing, found, names)
        self.assertEqual([r["deviceId"] for r in rows], [LAMP, BULBS[4], BULBS[0], STRIP])
        self.assertEqual(rows[0], {"name": "Desk Lamp", "deviceId": LAMP, "mode": "color", "segments": 1, "enabled": False})
        self.assertEqual(rows[3], {"name": "TV Strip", "deviceId": STRIP, "mode": "segments", "segments": 12,
                                   "channels": "rgb", "enabled": True})
        self.assertEqual(whites, [])
        self.assertEqual(rows[2]["mode"], "color")
        self.assertEqual((added, updated, missing), (["H6008 10.0.0.7", "TV Strip"], ["Desk Lamp"], ["Gone"]))

    def test_detect_lights_answers_a_merge_patch(self):
        fake = FakeLan({LAMP: {"ip": "10.0.0.5", "sku": "H6008", "status": {}},
                        STRIP: {"ip": "10.0.0.6", "sku": "H61E6", "status": {}}})
        shared, names = lan.shared_endpoint, main._names_from_home_assistant
        lan.shared_endpoint = lambda: fake
        main._names_from_home_assistant = lambda config=None: {STRIP: {"name": "TV Strip", "segments": 15}}
        try:
            answer = main.detect_lights({"config": {"devices": [{"name": "Lamp", "deviceId": LAMP}]}})
            self.assertEqual([r["deviceId"] for r in answer["mergePatch"]["devices"]], [LAMP, STRIP])
            self.assertIn("added TV Strip", answer["message"])
            again = main.detect_lights({"config": {"devices": answer["mergePatch"]["devices"]}})
            self.assertNotIn("mergePatch", again)
            self.assertIn("all already in the table", again["message"])
            fake.devices.clear()
            self.assertIn("No Govee light answered", main.detect_lights({})["message"])
        finally:
            lan.shared_endpoint, main._names_from_home_assistant = shared, names

    def test_segment_counts_come_from_the_capability(self):
        rgb = {"instance": "segmentedColorRgb",
               "parameters": {"fields": [{"fieldName": "segment", "elementRange": {"min": 0, "max": 11}}]}}
        self.assertEqual(ha.segment_count([{"instance": "colorRgb"}, rgb]), 12)
        self.assertEqual(ha.segment_count([{"instance": "segmentedColorRgb"}]), 15)
        self.assertEqual(ha.segment_count([{"instance": "colorRgb"}]), 0)


CEILING = ["0B:00:00:00:00:00:00:%02X" % i for i in range(1, 6)]
WHITE_RANGE = {"kelvin": (2000, 9000)}


class Rgbw(unittest.TestCase):
    """0.3.0: bulbs with a white mode as RGBW lights. W leads: white mode at a temperature from the frame; else RGB."""

    def test_white_leads_to_white_mode_at_the_frames_level(self):
        self.assertEqual(main.rgbw_command(0, 0, 0, 255, 100), ("white", 6504, 100))
        self.assertEqual(main.rgbw_command(0, 0, 0, 128, 100), ("white", 6504, 50), "a fade dims the bulb")
        self.assertEqual(main.rgbw_command(0, 0, 0, 255, 60)[2], 60, "under the Device brightness setting")
        self.assertEqual(main.rgbw_command(255, 0, 0, 0, 100), ("color", (255, 0, 0), 100))
        self.assertEqual(main.rgbw_command(0, 0, 0, 0, 100), ("color", (0, 0, 0), 100), "off stays a black colour")
        # A pastel mixes W into R, G and B in colour mode, at full brightness.
        self.assertEqual(main.rgbw_command(200, 0, 100, 55, 100), ("color", (255, 55, 155), 100))
        # Too dim for 1 % brightness: a dark colour instead of a 1 % white.
        self.assertEqual(main.rgbw_command(0, 0, 0, 1, 100)[0], "color")

    def test_the_tint_picks_the_temperature(self):
        warm = main.rgbw_command(105, 50, 0, 150, 100)  # Vixen's RGBW breakdown of (255, 200, 150)
        neutral = main.rgbw_command(0, 0, 0, 200, 100)
        cool = main.rgbw_command(0, 20, 60, 195, 100)  # (195, 215, 255)
        self.assertEqual([warm[0], neutral[0], cool[0]], ["white"] * 3)
        self.assertLess(warm[1], 4000)
        self.assertEqual(neutral[1], 6504, "sRGB white is D65")
        self.assertGreater(cool[1], 9000 - 1, "clamped to the top of the range")
        self.assertLess(main.kelvin_of(255, 140, 40), 2100, "deep amber is at the warm end")
        self.assertEqual(main.kelvin_of(255, 128, 128), 2000, "clamped to the bottom of the range")
        self.assertEqual(main.kelvin_of(200, 220, 255, (2700, 6500)), 6500, "the device's own range")
        self.assertEqual(main.rgbw_command(0, 0, 0, 255, 100, (2700, 6000))[1], 6000)

    def test_layout_names_and_lights(self):
        rows = [{"name": f"Ceiling {i + 1}", "deviceId": d, "channels": "rgbw"} for i, d in enumerate(CEILING)]
        devices, count = main.layout(rows)
        self.assertEqual(count, 20)
        self.assertEqual([(d.first_output, d.output_count) for d in devices][:2], [(0, 4), (4, 4)])
        answer = main.GoveeController.outputs_for({"devices": rows})
        self.assertEqual(answer["outputCount"], 20)
        self.assertEqual(answer["outputNames"][:5], ["Ceiling 1 R", "Ceiling 1 G", "Ceiling 1 B", "Ceiling 1 W", "Ceiling 2 R"])
        self.assertEqual(answer["lights"][0], {"name": "Ceiling 1", "outputCount": 4})
        # 0.2.x rows (no channels) stay RGB; Segments ignore RGBW (the stream carries RGB).
        mixed = [{"name": "Old", "deviceId": LAMP},
                 {"name": "Strip", "deviceId": STRIP, "mode": "segments", "segments": 2, "channels": "rgbw"}]
        answer = main.GoveeController.outputs_for({"devices": mixed})
        self.assertEqual(answer["outputCount"], 9)
        self.assertEqual([l["outputCount"] for l in answer["lights"]], [3, 3, 3])
        self.assertEqual(answer["lights"][1]["name"], "Strip S1")

    def test_detect_picks_rgbw_for_white_capable_lights(self):
        found = {CEILING[0]: {"ip": "10.0.0.7", "sku": "H601C"}, STRIP: {"ip": "10.0.0.6", "sku": "H61E6"},
                 LAMP: {"ip": "10.0.0.5", "sku": "H6008"}, BULBS[0]: {"ip": "10.0.0.8", "sku": "H6001"}}
        names = {CEILING[0]: dict(WHITE_RANGE, name="Kitchen 1"), STRIP: dict(WHITE_RANGE, name="TV Strip", segments=15),
                 LAMP: dict(WHITE_RANGE, name="Desk Lamp"), BULBS[0]: {"name": "Old Bulb", "kelvin": None}}
        existing = [{"name": "Desk Lamp", "deviceId": LAMP, "mode": "color", "segments": 1, "enabled": True}]
        rows, added, _, _, whites = main.merge_detected(existing, found, names)
        channels = {r["name"]: r.get("channels") for r in rows}
        self.assertEqual(channels, {"Desk Lamp": None, "Kitchen 1": "rgbw", "Old Bulb": "rgb", "TV Strip": "rgb"})
        self.assertEqual(whites, ["Desk Lamp"], "an existing RGB row is pointed out, never changed")
        fake = FakeLan({LAMP: {"ip": "10.0.0.5", "sku": "H6008", "status": {}}})
        shared, lookup = lan.shared_endpoint, main._names_from_home_assistant
        lan.shared_endpoint = lambda: fake
        main._names_from_home_assistant = lambda config=None: {LAMP: dict(WHITE_RANGE, name="Desk Lamp")}
        try:
            answer = main.detect_lights({"config": {"devices": existing}})
            self.assertIn("Desk Lamp has a white mode: set Channels to RGBW", answer["message"])
        finally:
            lan.shared_endpoint, main._names_from_home_assistant = shared, lookup

    def test_white_capability_from_home_assistant(self):
        cap = {"instance": "colorTemperatureK", "parameters": {"range": {"min": 2700, "max": 6500, "precision": 1}}}
        self.assertEqual(ha.kelvin_range([{"instance": "colorRgb"}, cap]), (2700, 6500))
        self.assertIsNone(ha.kelvin_range([{"instance": "colorRgb"}]))


class RgbwShow(unittest.TestCase):
    """An RGBW lamp against the fake LAN (Controller's fixtures, without re-running its tests)."""
    setUp, tearDown, make = Controller.setUp, Controller.tearDown, Controller.make

    def frame(self, c, values):
        c.frame(vp.Frame({"encoding": "u8", "values": base64.b64encode(bytes(values)).decode()}))

    def run_show(self, rows, frames, outputs, **extra):
        c = self.make(settings(rows, **extra), outputs)
        c.start()
        deadline = time.time() + 3
        while time.time() < deadline and not any(s["state"] == "running" for s in self.studio.of("controller.status")):
            time.sleep(0.02)
        for values in frames:
            self.frame(c, values)
            time.sleep(0.15)
        c.running = False
        c.stop()
        return [m for m in self.lan.to("10.0.0.5")]

    def test_white_frame_plays_white_mode_and_red_plays_rgb(self):
        sent = self.run_show([{"name": "Lamp", "deviceId": LAMP, "channels": "rgbw"}],
                             [[0, 0, 0, 255], [0, 0, 0, 128], [255, 0, 0, 0]], 4,
                             keepAliveMs=0, maxFps=40, onStop="leave")
        show = [m for m in sent if m["cmd"] in ("colorwc", "brightness")]
        start = show.index({"cmd": "colorwc", "data": {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 6504}})
        self.assertEqual(show[start - 1], {"cmd": "brightness", "data": {"value": 100}}, "show start")
        self.assertEqual(show[start + 1:start + 3], [
            {"cmd": "brightness", "data": {"value": 50}},
            {"cmd": "colorwc", "data": {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 6504}}],
            "a dimmer white lowers the brightness first")
        self.assertEqual(show[start + 3:start + 5], [
            {"cmd": "colorwc", "data": {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0}},
            {"cmd": "brightness", "data": {"value": 100}}],
            "colour plays in RGB at full brightness, colour first")

    def test_keepalive_puts_the_brightness_back(self):
        # Something else (Adaptive Lighting) dims the lamp mid-show: the keepalive resends the brightness with the colour.
        sent = self.run_show([{"name": "Lamp", "deviceId": LAMP}], [[255, 255, 255]] * 4, 3,
                             keepAliveMs=100, maxFps=40, onStop="leave")
        keepalives = [i for i, m in enumerate(sent) if m["cmd"] == "colorwc"][1:]
        self.assertTrue(keepalives)
        self.assertTrue(all(sent[i + 1] == {"cmd": "brightness", "data": {"value": 100}} for i in keepalives[:-1]))
