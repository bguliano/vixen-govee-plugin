# Govee Output for Vixen Studio

A [Vixen Studio](https://github.com/bguliano/vixen-macos-studio) plugin that adds a **Govee (LAN)** controller type:
patch elements to it and Govee lights play in your show, over Govee's local LAN API. Nothing goes through the cloud.

- **One colour per light** for bulbs, lamps and downlights (`colorwc`), as **RGB** or, for bulbs with a white mode, **RGBW**:
  white plays in the bulb's own (much brighter) white mode at a colour temperature worked out from the frame.
- **Per-segment colour** for strips that support Govee's DreamView stream (the undocumented `razer` channel).
- Devices are found by their **Govee device id** on every start, so DHCP address changes don't break the show.
- Sends **only on change**, capped per device (default 20 updates a second), with a once-a-second resend to cover a
  lost UDP packet.
- When the show stops, each light goes **back to what it was** before the show (power, colour or white, brightness).
- Optional **Home Assistant Light Arbiter** handoff: the plugin asks `script.light_acquire` for the lights when the show
  starts and `script.light_release` when it stops, so automations (Adaptive Lighting, effects) don't fight the show.

## Requirements

- Vixen Studio with the plugin system (plugin API 1.0; 1.1, from 3.13.4 alpha 5, adds Detect Lights and outputs that follow the table).
- `python3` on the Mac (macOS's own, from the Command Line Tools, is fine; Python 3.9+, no packages needed).
- **LAN Control** turned on for each light in the Govee Home app (device ▸ settings ▸ LAN Control).
- The Mac running the show on the **same network** as the lights (not a guest network). Discovery uses multicast;
  if your router blocks it, list the light under Fixed addresses.

## Install

1. Download `io.github.bguliano.govee-output-<version>.vixenplugin` (check it with `shasum -a 256` against the
   `.sha256` beside it).
2. Vixen Studio ▸ Settings ▸ Plugins ▸ **Install from File…** (or drop the file on the list). The sheet lists what
   the plugin may do:
   - *Send and receive UDP on your local network*: the Govee LAN protocol (ports 4001–4003).
   - *Make HTTP requests to homeassistant.local:8123*: only for the optional Light Arbiter handoff.
   - *Keep its own settings in its data folder*: found addresses and the optional Home Assistant token.

## Set up a controller

1. In the show window's **Controllers** section, Add Controller ▸ Plugins ▸ **Govee (LAN)**.
2. Press **Detect Lights** in the controller's inspector. It finds every Govee light on the network and adds it to
   the **Devices** table: bulbs, lamps and downlights as *One colour* (with **RGBW** channels when they have a white
   mode), strips that support it as *Segments* with their segment count (names, segment counts and white modes come
   from Home Assistant once it's set up below). Rows already in the table
   keep their settings, so it's safe to press again after adding a light. You can also fill in rows by hand.
3. The controller's outputs follow the table: 3 outputs (R, G, B) per light, 4 (R, G, B, W) for an RGBW light, or 3
   per segment in *Segments* mode, named after each light (`Ceiling R`, `Ceiling G`, `Ceiling B`, `Ceiling W`,
   `TV Strip S1 R`, …). Removing a row whose outputs are
   patched asks first; Undo puts everything back. Disabled rows keep their outputs, so turning a light off never moves
   the patching after it.
4. Patch your elements to the outputs (RGB colour handling, RGBW for RGBW lights; Display ▸ Add ▸ Single Bulbs… ▸ Use
   the controller's Lights does both in one step), turn on Live Output and use the Test submenu.

**Vixen Studio before 3.13.4 alpha 5** has no Detect Lights button and doesn't size the controller: use Plugins ▸
Govee Output ▸ **Suggest Devices…** for a ready table and the outputs it needs, set the controller's outputs to that
number, then paste or fill in the table. **Discover Govee Devices…** just lists what answers.

An agent connected to Studio's MCP server can do all of this: `controller_create`, then `controller_action
{action: "detect"}` (or `plugin__govee_output__suggest_devices` and `controller_configure {config}`), `patch_elements`,
then `plugin__govee_output__identify` to flash one light and find it in the room (needs Live Output on).

## Settings

| Setting | Default | |
|---|---|---|
| Devices | empty | Name, Govee device id, mode (*One colour*, *Segments* or *Segments, reversed*), channels (*RGB* or *RGBW*, One colour only), segment count, on. Detect Lights fills it in. |
| Device brightness | 100 % | Set when the show starts and resent with the keepalive (so a light Adaptive Lighting dims mid-show comes back); the show's levels scale the colours under it. |
| Updates per second per device | 20 | Govee firmware drops commands sent too fast. Lower it if a light stutters. |
| Resend unchanged colour every | 1000 ms | 0 sends only on change. |
| Gamma | 1 | Above 1 deepens dim levels (LEDs look bright at low values). |
| Blend between segments | off | DreamView's gradient flag. |
| When the show stops | Put each light back | Or leave the last colour. |
| Fixed addresses | none | Only for a light discovery can't find: `<device id> <IP>` per entry. |
| Home Assistant | off | URL, requester (the Light Arbiter's list), reason, force. Lights are matched to Home Assistant by Govee id. |

## RGBW lights (white mode)

Govee's colour-temperature bulbs, lamps and downlights (H601C, H6008 and the like) have a white mode that is much
brighter than white mixed from red, green and blue, but they run **one mode at a time**: white *or* colour. Set a
light's **Channels** to *RGBW* and it takes four outputs (`Ceiling R`, `G`, `B`, `W`); in Vixen it's an ordinary RGBW
light (RGBW colour handling), and Vixen's RGBW breakdown puts white on W. Each frame, the plugin picks the mode:

- **W leads** (W ≥ the largest of R, G and B, which includes W alone): **white mode**. The colour temperature comes from
  the frame's whole colour, W plus any tint, as a point on the white scale: plain white is 6500 K (sRGB's white),
  a reddish or amber tint is warmer (down to the device's lowest, 2000 K), a bluish tint cooler (up to its highest,
  9000 K); the range comes from Home Assistant when the Light Arbiter is on. The frame's level sets the device
  brightness (under the Device brightness setting), so a white fade dims the bulb. Sent as `brightness`, then
  `colorwc` with `colorTemInKelvin`.
- **Otherwise: colour mode**, with W mixed into R, G and B (so pastels stay close) at the Device brightness setting.
  Sent as `colorwc` with the colour, then `brightness`.

Switching modes is instant but not blended, so a crossfade from white to a colour jumps where the colour takes the
lead. Rows saved by 0.2.x have no Channels and stay RGB; Detect Lights never changes an existing row's channels (that
would move the outputs of every light after it) but says which ones have a white mode. Segments rows are always RGB:
the segment stream carries colour only.

## Segments

*Segments* mode streams one colour per segment over DreamView: arm, wait about a second, then frames. Not every
model has it (strips that offer DreamView or "Razer"/"Sync" in the Govee app usually do), and the number of segments
the stream addresses can differ from what the Govee app shows. Detect Lights starts from what Home Assistant reports
(15 on the H61E6) and you adjust with a moving-pixel test pattern. If a strip stays dark in Segments mode, switch it to *One colour*.

## Home Assistant Light Arbiter (optional)

For homes using the Light Arbiter scripts (`script.light_acquire` / `script.light_release`):

1. In Home Assistant, Profile ▸ Security ▸ **Long-lived access tokens** ▸ Create token.
2. Save it where only the plugin reads it (paste the token at the prompt; it isn't echoed or kept in your history):

   ```sh
   D="$HOME/Library/Application Support/Vixen Studio/Plugins/data/io.github.bguliano.govee-output"
   mkdir -p "$D" && chmod 700 "$D" && read -rs "T?Home Assistant token: " && printf %s "$T" > "$D/ha-token" && chmod 600 "$D/ha-token"; unset T; echo
   ```

3. In the controller's settings, turn on **Use the Light Arbiter** and set the URL (port 8123; the plugin's network
   permission covers port 8123 only). Each light is matched to its Home Assistant light by Govee id (govee2mqtt's
   status sensors), so there's nothing to fill in per row.
4. Plugins ▸ Govee Output ▸ **Check Home Assistant…** confirms the URL and token.

Lights another requester holds are left alone (with a message) unless **Force** is on. If Home Assistant can't be
reached, the show still plays and Studio shows why. Release runs after the lights are restored, so Adaptive Lighting
takes over again straight away.

## Development

```sh
python3 -m unittest discover -s tests -v                   # packets, RGBW, the controller against fake devices, UDP, HA
vixen-studio-mcp plugin validate .                         # Studio's conformance harness
vixen-studio-mcp plugin run . --frames chase --outputs 72 --config "$(cat my-config.json)"   # real output!
vixen-studio-mcp plugin pack . --out dist                  # .vixenplugin + .sha256
```

Link the folder for live development in Settings ▸ Plugins ▸ Link Development Folder… (Studio restarts the plugin
when a file changes). Logs: Settings ▸ Plugins ▸ Govee Output ▸ Log…, or
`~/Library/Application Support/Vixen Studio/Plugins/logs/io.github.bguliano.govee-output.log`.

`vixen_plugin.py` is Studio's Python SDK, copied unchanged.

## Protocol notes

Govee LAN: multicast `scan` to 239.255.255.250:4001, answers on 4002, commands to the device on 4003 (`turn`,
`brightness`, `colorwc`, `devStatus`). DreamView frames: `BB <len16> <opcode> <payload> <xor>`, arm with
`BB 00 01 B1 01 0A`, colours with opcode `B0` and payload `[gradient, count, R,G,B × count]`. Thanks to the
reverse-engineering in LedFx, govee2mqtt, OpenRGB and govee-local-api.

## Licence

MIT.
