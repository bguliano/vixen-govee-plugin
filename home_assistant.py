"""A small Home Assistant REST client: just enough to hand lights to a show through the Light Arbiter and to name the
Govee devices discovery finds. The token lives in the plugin's data folder (ha-token), never in the show."""
import json
import os
import urllib.error
import urllib.request

TOKEN_FILE = "ha-token"


class HomeAssistantError(Exception):
    pass


def token_path(data_dir):
    return os.path.join(data_dir or "", TOKEN_FILE)


def read_token(data_dir):
    try:
        with open(token_path(data_dir), encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    return token or None


class Client:
    def __init__(self, url, token, timeout=5.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method)
        request.add_header("Authorization", "Bearer " + self.token)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise HomeAssistantError("Home Assistant refused the token (401): make a new long-lived token") from None
            raise HomeAssistantError(f"Home Assistant answered {error.code} for {path}") from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise HomeAssistantError(f"can't reach Home Assistant at {self.url}: {reason}") from None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise HomeAssistantError(f"Home Assistant sent something that isn't JSON for {path}") from None

    def check(self):
        """{"message": "API running."} when the URL and token work."""
        return self._request("GET", "/api/")

    def states(self):
        return self._request("GET", "/api/states") or []

    def call_service(self, domain, service, data, return_response=False):
        path = f"/api/services/{domain}/{service}" + ("?return_response" if return_response else "")
        answer = self._request("POST", path, data)
        if return_response and isinstance(answer, dict):
            return answer.get("service_response")
        return answer

    def acquire(self, lights, requester, reason, force):
        """Light Arbiter: Acquire. Returns {"granted": [...], "denied": [...], "denied_owners": [...]}."""
        return self.call_service("script", "light_acquire",
                                 {"lights": lights, "requester": requester, "reason": reason, "force": force},
                                 return_response=True) or {}

    def release(self, lights, requester, reason, force):
        return self.call_service("script", "light_release",
                                 {"lights": lights, "requester": requester, "reason": reason, "force": force},
                                 return_response=True) or {}

    def govee_devices(self):
        """Govee devices govee2mqtt publishes: {device_id: {"name", "sku", "entity"}}, matched to their light entity
        through the status sensor's platform metadata and the shared device name prefix."""
        states = self.states()
        lights = {s["entity_id"]: s for s in states if s.get("entity_id", "").startswith("light.")}
        found = {}
        for s in states:
            attrs = s.get("attributes") or {}
            meta = attrs.get("platform_metadata")
            if not isinstance(meta, dict) or not meta.get("device"):
                continue
            name = (attrs.get("friendly_name") or "").removesuffix(" Status").strip()
            entity = next((eid for eid, light in lights.items()
                           if (light.get("attributes") or {}).get("friendly_name") == name), None)
            capabilities = meta.get("capabilities") or []
            found[meta["device"].upper()] = {"name": name, "sku": meta.get("sku"), "entity": entity,
                                             "segments": segment_count(capabilities)}
        return found


def segment_count(capabilities):
    """How many segments a device's segmentedColorRgb capability addresses (its "segment" field's element range), 0
    without the capability. When the range isn't listed, 15: what govee2mqtt reports for most strips."""
    for capability in capabilities:
        if not isinstance(capability, dict) or capability.get("instance") != "segmentedColorRgb":
            continue
        fields = (capability.get("parameters") or {}).get("fields") or []
        for field in fields:
            if isinstance(field, dict) and field.get("fieldName") == "segment":
                span = field.get("elementRange") or {}
                if isinstance(span.get("max"), int) and isinstance(span.get("min"), int) and span["max"] >= span["min"]:
                    return span["max"] - span["min"] + 1
                options = field.get("options")
                if isinstance(options, list) and options:
                    return len(options)
        return 15
    return 0
