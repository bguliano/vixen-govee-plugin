#!/usr/bin/env python3
"""Govee Output: Govee lights as a Vixen Studio controller, over Govee's LAN API.

Each device in the controller's Devices table takes three outputs (R, G, B) for one colour, or three per segment when
its mode is "segments" (the DreamView/razer stream, for strips that support it). Devices are found by their Govee
device id on every start, so a new DHCP lease doesn't lose them. Optionally the plugin hands the lights to the show
through a Home Assistant "Light Arbiter" (scripts light_acquire / light_release) and gives them back afterwards.
"""
import threading
import time

import govee_lan as lan
import home_assistant as ha
import vixen_plugin as vp

plugin = vp.Plugin()
controllers = {}  # instance id -> GoveeController, for the tools

RESCAN_SECONDS = 30.0
REARM_SECONDS = 30.0


def _endpoint():
    try:
        return lan.shared_endpoint()
    except OSError as error:
        raise vp.ToolError(f"can't open UDP port {lan.LISTEN_PORT} for Govee answers: {error}") from None


def _home_assistant(config):
    """A client when Home Assistant is turned on in the settings and a token is saved; else None."""
    settings = config.get("homeAssistant") or {}
    if not settings.get("enabled"):
        return None
    token = ha.read_token(plugin.data_dir)
    if not token:
        raise ha.HomeAssistantError(f"no token: save a long-lived token in {ha.token_path(plugin.data_dir)}")
    return ha.Client(settings["url"], token)


class Device:
    """One row of the Devices table and what the show last asked of it."""

    def __init__(self, row, first_output):
        self.name = row.get("name") or row["deviceId"]
        self.device_id = lan.normalise_device_id(row["deviceId"])
        self.configured_ip = row.get("ip") or None
        self.ip = self.configured_ip
        self.mode = row.get("mode", "color")
        self.segments = row.get("segments", 1) if self.mode == "segments" else 1
        self.reverse = bool(row.get("reverse", False))
        self.enabled = row.get("enabled", True)
        self.entity = row.get("haEntity") or None
        self.first_output = first_output
        self.output_count = 3 * self.segments
        self.reset()

    def reset(self):
        self.found = self.ip is not None
        self.wanted = None  # list of (r, g, b): one, or one per segment
        self.sent = None
        self.last_send = 0.0
        self.armed_at = None
        self.ready_at = 0.0
        self.prior = None  # devStatus captured at start, for restore
        self.skipped = None  # why the show doesn't drive it (arbiter refused), or None
        self.sends = 0

    def colors_from(self, values, gamma_table):
        chunk = values[self.first_output:self.first_output + self.output_count]
        chunk = list(chunk) + [0] * (self.output_count - len(chunk))
        colors = [tuple(gamma_table[v & 0xFF] for v in chunk[i:i + 3]) for i in range(0, self.output_count, 3)]
        return colors[::-1] if self.reverse else colors

    def packet(self, colors, blend):
        if self.mode == "segments":
            return lan.razer_colors_message(colors, gradient=blend)
        return lan.color_message(*colors[0])

    def describe(self):
        return {
            "name": self.name, "deviceId": self.device_id, "ip": self.ip, "found": self.found, "mode": self.mode,
            "segments": self.segments, "outputs": f"{self.first_output + 1}-{self.first_output + self.output_count}",
            "enabled": self.enabled, "haEntity": self.entity, "skipped": self.skipped, "packetsSent": self.sends,
            "lastSendAgeMs": None if not self.last_send else int((time.monotonic() - self.last_send) * 1000),
        }


def layout(devices_config):
    """Devices with their output ranges, in table order. Disabled devices keep their outputs, so turning one off
    never moves the patching of the ones after it."""
    devices, next_output = [], 0
    for row in devices_config:
        device = Device(row, next_output)
        devices.append(device)
        next_output += device.output_count
    return devices, next_output


@plugin.controller()
class GoveeController(vp.Controller):
    def configure(self, config):
        errors, seen = [], set()
        for i, row in enumerate(config["devices"]):
            device_id = lan.normalise_device_id(row["deviceId"])
            if not device_id:
                errors.append({"field": f"/devices/{i}/deviceId",
                               "message": "must be a Govee device id, eight bytes like 1A:2B:3C:4D:5E:6F:70:81"})
            elif device_id in seen:
                errors.append({"field": f"/devices/{i}/deviceId", "message": "is already in the table"})
            seen.add(device_id)
        if errors:
            raise vp.ConfigError(errors)
        config = dict(config, devices=[dict(row, deviceId=lan.normalise_device_id(row["deviceId"]))
                                       for row in config["devices"]])
        devices, needed = layout(config["devices"])
        if needed > self.output_count:
            raise vp.ConfigError(f"these devices need {needed} outputs and the controller has {self.output_count}: "
                                 f"set its outputs to {needed}", field="/devices")
        with self._lock_obj():
            self.settings = config
            self._apply(devices)
        return config

    def _lock_obj(self):
        if not hasattr(self, "lock"):
            self.lock = threading.Condition()
            self.devices = []
            self.worker = None
            self.stopping = False
            self.acquired = []
            controllers[self.instance_id] = self
        return self.lock

    def _apply(self, devices):
        """New settings; while running, devices that are still there keep their address and state."""
        old = {d.device_id: d for d in self.devices}
        for device in devices:
            previous = old.get(device.device_id)
            if previous is not None and self.running:
                device.ip = device.configured_ip or previous.ip
                device.found, device.prior, device.skipped = previous.found, previous.prior, previous.skipped
                device.armed_at = previous.armed_at if device.mode == previous.mode == "segments" else None
        self.devices = devices
        gamma = float(self.settings.get("gamma", 1.0))
        self.gamma_table = [round(255 * ((v / 255) ** gamma)) for v in range(256)]
        self.lock.notify_all()

    def set_output_count(self, count):
        self.output_count = count
        needed = sum(d.output_count for d in getattr(self, "devices", []))
        if needed > count:
            self.status("error", f"the devices need {needed} outputs and the controller has {count}")

    def output_names(self):
        names = []
        for device in getattr(self, "devices", []):
            for s in range(device.segments):
                label = device.name if device.segments == 1 else f"{device.name} S{s + 1}"
                names += [f"{label} R", f"{label} G", f"{label} B"]
        return names

    # ---- Live output ----------------------------------------------------------------------------------------------

    def start(self):
        with self._lock_obj():
            self.stopping = False
            for device in self.devices:
                device.ip = device.configured_ip
                device.reset()
        self.status("unavailable", "finding devices on the network…")
        self.worker = threading.Thread(target=self._run, name=f"govee-{self.instance_id[:8]}", daemon=True)
        self.worker.start()

    def frame(self, frame):
        values = frame.values
        with self.lock:
            for device in self.devices:
                device.wanted = device.colors_from(values, self.gamma_table)
            self.lock.notify_all()

    def stop(self):
        with self._lock_obj():
            self.stopping = True
            self.lock.notify_all()
        if self.worker is not None:
            self.worker.join(timeout=4.0)
            self.worker = None
        self._hand_back()

    def destroy(self):
        controllers.pop(self.instance_id, None)

    # ---- The worker -----------------------------------------------------------------------------------------------

    def _run(self):
        try:
            endpoint = lan.shared_endpoint()
        except OSError as error:
            self.status("error", f"can't open UDP port {lan.LISTEN_PORT}: {error}")
            return
        self._find(endpoint)
        self._take_over(endpoint)
        last_scan = last_stats = time.monotonic()
        sent_packets = sent_bytes = errors = 0
        while True:
            with self.lock:
                if self.stopping:
                    break
                settings, devices = self.settings, list(self.devices)
            now = time.monotonic()
            interval = 1.0 / float(settings["maxFps"])
            keepalive = settings["keepAliveMs"] / 1000.0
            next_due = now + 1.0
            for device in devices:
                if not (device.enabled and device.found and device.skipped is None):
                    continue
                try:
                    if device.mode == "segments" and (device.armed_at is None or now - device.armed_at > REARM_SECONDS):
                        endpoint.send(device.ip, lan.razer_arm_message(True))
                        if device.armed_at is None:
                            device.ready_at = now + lan.RAZER_ARM_DELAY
                        device.armed_at = now
                    if now < device.ready_at or device.wanted is None:
                        next_due = min(next_due, max(device.ready_at, now + 0.05))
                        continue
                    changed = device.wanted != device.sent
                    due = device.last_send + (interval if changed else keepalive)
                    if (changed or keepalive > 0) and now >= due:
                        packet = device.packet(device.wanted, settings.get("segmentBlend", False))
                        endpoint.send(device.ip, packet)
                        device.sent, device.last_send = device.wanted, now
                        device.sends += 1
                        sent_packets += 1
                        sent_bytes += len(packet)
                        due = now + interval
                    if changed or keepalive > 0:
                        next_due = min(next_due, max(due, now + 0.001))
                except OSError as error:
                    errors += 1
                    device.found = False
                    plugin.warn(f"{device.name}: send failed ({error}); looking for it again")
            if now - last_scan > RESCAN_SECONDS and any(d.enabled and not d.found for d in devices):
                self._find(endpoint, quiet=True)
                last_scan = time.monotonic()
            if now - last_stats >= 1.0:
                self.stats(packets_sent=sent_packets, bytes_sent=sent_bytes, errors=errors)
                sent_packets = sent_bytes = errors = 0
                last_stats = now
            with self.lock:
                if not self.stopping:
                    self.lock.wait(max(0.0, next_due - time.monotonic()))

    def _find(self, endpoint, quiet=False):
        """Resolves each enabled device's address by its id with a multicast scan; a configured IP is the fallback."""
        with self.lock:
            devices = [d for d in self.devices if d.enabled]
        if not devices:
            self.status("running", "no devices in the table")
            return
        found = endpoint.scan(timeout=2.0)
        cache = {}
        for device in devices:
            answer = found.get(device.device_id)
            if answer:
                device.ip, device.found = answer["ip"], True
                cache[device.device_id] = answer["ip"]
            elif device.configured_ip:
                device.ip, device.found = device.configured_ip, True
        if cache:
            try:
                known = plugin.storage_get("addresses", {}) or {}
                known.update(cache)
                plugin.storage_set("addresses", known)
            except (vp.RpcError, TimeoutError):
                pass
        missing = [d.name for d in devices if not d.found]
        if not missing:
            self.status("running", f"{len(devices)} Govee device{'s' if len(devices) != 1 else ''} on the LAN")
        elif len(missing) == len(devices):
            self.status("unavailable", "no Govee device answered: is LAN Control on in the Govee app, and is this Mac "
                                       "on the same network?")
        else:
            self.status("running", f"{len(devices) - len(missing)} of {len(devices)} found; missing: " + ", ".join(missing))
        if missing and not quiet:
            plugin.warn("not found on the LAN yet: " + ", ".join(missing))

    def _take_over(self, endpoint):
        """Arbiter first (so nothing else fights the show), then remember each light's state and switch it on."""
        settings = self.settings
        try:
            client = _home_assistant(settings)
        except ha.HomeAssistantError as error:
            client = None
            self.message("Govee Output: Home Assistant", f"{error}. The show will drive the lights without asking "
                                                          "the Light Arbiter.")
        if client is not None:
            arbiter = settings["homeAssistant"]
            lights = [d.entity for d in self.devices if d.enabled and d.found and d.entity]
            if lights:
                try:
                    answer = client.acquire(lights, arbiter["requester"], arbiter["reason"], arbiter["force"])
                    self.acquired = list(answer.get("granted") or [])
                    denied = dict(zip(answer.get("denied") or [], answer.get("denied_owners") or []))
                    for device in self.devices:
                        if device.entity in denied:
                            device.skipped = f"held by {denied[device.entity]}"
                    if denied:
                        self.message("Govee Output: lights in use",
                                     "The Light Arbiter kept these lights for their current owner, so the show leaves "
                                     "them alone: " + ", ".join(f"{e} ({o})" for e, o in denied.items())
                                     + ". Turn on Force in the controller's Home Assistant settings to take them anyway.")
                except ha.HomeAssistantError as error:
                    self.message("Govee Output: Home Assistant", f"{error}. The show will drive the lights anyway.")
        live = [d for d in self.devices if d.enabled and d.found and d.skipped is None]
        priors = {}

        def capture(device):
            priors[device.device_id] = endpoint.status(device.ip, timeout=1.0)

        threads = [threading.Thread(target=capture, args=(d,), daemon=True) for d in live]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1.5)
        brightness = settings["brightness"]
        for device in live:
            device.prior = priors.get(device.device_id)
            try:
                if not device.prior or device.prior.get("onOff") != 1:
                    endpoint.send(device.ip, lan.turn_message(True))
                if not device.prior or device.prior.get("brightness") != brightness:
                    endpoint.send(device.ip, lan.brightness_message(brightness))
            except OSError as error:
                plugin.warn(f"{device.name}: {error}")

    def _hand_back(self):
        """Leaves the lights as they were before the show (when asked), then releases them to the arbiter."""
        settings = getattr(self, "settings", {})
        try:
            endpoint = lan.shared_endpoint()
        except OSError:
            return
        for device in self.devices:
            if not device.found or device.skipped is not None:
                continue
            try:
                if device.mode == "segments" and device.armed_at is not None:
                    endpoint.send(device.ip, lan.razer_arm_message(False))
                prior = device.prior
                if settings.get("onStop") == "restore" and prior:
                    if prior.get("onOff") == 0:
                        endpoint.send(device.ip, lan.turn_message(False))
                    else:
                        kelvin = prior.get("colorTemInKelvin") or 0
                        color = prior.get("color") or {}
                        if kelvin:
                            endpoint.send(device.ip, lan.kelvin_message(kelvin))
                        else:
                            endpoint.send(device.ip, lan.color_message(color.get("r", 255), color.get("g", 255),
                                                                       color.get("b", 255)))
                        if prior.get("brightness"):
                            endpoint.send(device.ip, lan.brightness_message(prior["brightness"]))
            except OSError as error:
                plugin.warn(f"{device.name}: couldn't restore ({error})")
            device.armed_at = None
        if self.acquired:
            arbiter = settings.get("homeAssistant") or {}
            try:
                client = _home_assistant(settings)
                if client is not None:
                    client.release(self.acquired, arbiter["requester"], "Vixen show ended", arbiter["force"])
            except ha.HomeAssistantError as error:
                plugin.warn(f"couldn't release the lights in Home Assistant: {error}")
            self.acquired = []


# ---- Agent tools (also in the Plugins menu) ----------------------------------------------------------------------

def _names_from_home_assistant(config=None):
    """Govee devices known to Home Assistant, when a URL and token are available; else {}."""
    settings = (config or {}).get("homeAssistant") or {}
    url = settings.get("url")
    if not url:
        url = next((c.settings["homeAssistant"]["url"] for c in controllers.values()
                    if getattr(c, "settings", None)), None)
    token = ha.read_token(plugin.data_dir)
    if not (url and token):
        return {}
    try:
        return ha.Client(url, token).govee_devices()
    except ha.HomeAssistantError as error:
        plugin.warn(f"device names from Home Assistant unavailable: {error}")
        return {}


@plugin.tool("discover")
def discover(arguments):
    timeout = (arguments or {}).get("timeoutMs", 2000) / 1000.0
    found = _endpoint().scan(timeout=timeout)
    names = _names_from_home_assistant(arguments)
    configured = {d.device_id: (c, d) for c in controllers.values() for d in getattr(c, "devices", [])}
    rows = []
    for device_id, answer in sorted(found.items(), key=lambda kv: kv[1].get("ip", "")):
        known = names.get(device_id) or {}
        rows.append({"deviceId": device_id, "ip": answer.get("ip"), "sku": answer.get("sku"),
                     "name": known.get("name"), "haEntity": known.get("entity"),
                     "inShow": device_id in configured})
    missing = [d.name for _, d in configured.values() if d.enabled and d.device_id not in found]
    if not rows:
        return ("No Govee device answered on the LAN. Check that LAN Control is on for each device in the Govee app "
                "and that this Mac is on the same network (not a guest network or VPN-only link).")
    return {"found": rows, "configuredButMissing": missing}


@plugin.tool("suggest_devices")
def suggest_devices(arguments):
    """A Devices table for every Govee light on the LAN, named from Home Assistant when it's set up."""
    arguments = arguments or {}
    found = _endpoint().scan(timeout=arguments.get("timeoutMs", 2000) / 1000.0)
    names = _names_from_home_assistant(arguments)
    rows = []
    for device_id, answer in sorted(found.items(), key=lambda kv: (names.get(kv[0], {}).get("name") or kv[1]["sku"])):
        known = names.get(device_id) or {}
        segmented = known.get("segments") and arguments.get("segments", True)
        rows.append({"name": known.get("name") or f"{answer.get('sku')} {answer.get('ip')}", "deviceId": device_id,
                     "ip": "", "mode": "segments" if segmented else "color", "segments": 15 if segmented else 1,
                     "reverse": False, "enabled": True, "haEntity": known.get("entity") or ""})
    devices, outputs = layout(rows)
    return {"devices": rows, "outputsNeeded": outputs,
            "note": "Segment counts default to 15 (what govee2mqtt reports for most strips); check each strip with "
                    "a test pattern and adjust. Set the controller's outputs to outputsNeeded before pasting."}


@plugin.tool("status")
def status(arguments):
    if not controllers:
        return "No Govee controller is set up in this show yet."
    return {iid: {"running": c.running, "devices": [d.describe() for d in getattr(c, "devices", [])],
                  "acquiredInHomeAssistant": getattr(c, "acquired", [])}
            for iid, c in controllers.items()}


@plugin.tool("identify")
def identify(arguments):
    if plugin.muted:
        raise vp.ToolError("Live Output is off: turn it on in Studio first, since this flashes a real light.")
    device_id = lan.normalise_device_id((arguments or {}).get("deviceId", ""))
    if not device_id:
        raise vp.ToolError("deviceId must be a Govee device id like 1A:2B:3C:4D:5E:6F:70:81")
    endpoint = _endpoint()
    found = endpoint.scan(timeout=1.5)
    if device_id not in found:
        raise vp.ToolError(f"{device_id} didn't answer on the LAN")
    ip = found[device_id]["ip"]
    prior = endpoint.status(ip) or {}
    for _ in range(3):
        endpoint.send(ip, lan.color_message(255, 255, 255))
        time.sleep(0.35)
        endpoint.send(ip, lan.color_message(0, 0, 0))
        time.sleep(0.35)
    kelvin, color = prior.get("colorTemInKelvin") or 0, prior.get("color") or {"r": 255, "g": 255, "b": 255}
    endpoint.send(ip, lan.kelvin_message(kelvin) if kelvin else lan.color_message(color["r"], color["g"], color["b"]))
    if prior.get("onOff") == 0:
        endpoint.send(ip, lan.turn_message(False))
    return f"Flashed {device_id} at {ip} three times and put it back."


@plugin.tool("home_assistant_check")
def home_assistant_check(arguments):
    url = (arguments or {}).get("url") or next(
        (c.settings["homeAssistant"]["url"] for c in controllers.values() if getattr(c, "settings", None)), None)
    if not url:
        raise vp.ToolError("give url, or set it in a Govee controller's Home Assistant settings")
    token = ha.read_token(plugin.data_dir)
    if not token:
        raise vp.ToolError(f"no token saved: put a Home Assistant long-lived token in {ha.token_path(plugin.data_dir)} "
                           "(see the plugin's README)")
    try:
        ha.Client(url, token).check()
        devices = ha.Client(url, token).govee_devices()
    except ha.HomeAssistantError as error:
        raise vp.ToolError(str(error)) from None
    return f"Home Assistant at {url} accepts the token and knows {len(devices)} Govee device(s)."


@plugin.event("liveOutput.changed")
def live_output_changed(data):
    plugin.info("live output is " + ("off" if plugin.muted else "on"))


if __name__ == "__main__":
    plugin.run()
