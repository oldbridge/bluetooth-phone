# An old rotary-phone as Bluetooth set
<a href="https://hackaday.io/project/165208-an-old-rotary-phone-as-bluetooth-set"><img src="https://cdn.hackaday.io/images/6515591556216314998.jpg" title="Bluetooth Rotary Phone" alt="BluetoothRotaryPhone"></a>

This project turns a vintage rotary phone into a GSM/Bluetooth-style handset using a Raspberry Pi.
The Python service reads rotary pulses and hook state from GPIO, interacts with `oFono` over D-Bus,
and plays local WAV prompts through ALSA.

It supports:

- Manual dialing when the handset is lifted
- Speed-dial shortcuts when the handset is on-hook
- Incoming-call ringing control (via a GPIO-driven ringer relay)
- Pick-up to answer and hang-up to end call
- Utility actions such as ringer test and system shutdown

## Prerequisites

You will need these Python modules available for `telefonoa.py`:

```
dbus
alsaaudio
yaml
RPi.GPIO
```

In practice, this usually means installing:

- `python3-dbus`
- `python3-yaml`
- `python3-rpi.gpio`
- ALSA userspace and Python binding (`pyalsaaudio`)
- `oFono` running and exposing `org.ofono` on the system bus

## Quick start (Raspberry Pi OS)

From the project directory (`bluetooth-phone/`):

```bash
sudo apt update
sudo apt install -y python3-dbus python3-yaml python3-rpi.gpio python3-dev libasound2-dev build-essential ofono
python3 -m pip install --upgrade pip
python3 -m pip install pyalsaaudio
```

Then run:

```bash
python3 telefonoa.py
```

## Small oFono configuration

Minimal setup to let this script access calls through D-Bus:

1. Enable and start the service:

```bash
sudo systemctl enable --now ofono
```

2. Add your runtime user to the `ofono` group (required for `GetModems` access):

```bash
sudo usermod -aG ofono $USER
```

3. Re-login (or reboot) so new groups are applied.

4. Confirm `oFono` sees a modem:

```bash
sudo dbus-send --system --print-reply --dest=org.ofono / org.ofono.Manager.GetModems
```

If the returned array is empty, `oFono` is running but no modem is ready yet.

## What the script does

`telefonoa.py` is the main runtime service. It contains:

- `RotaryDial`: decodes rotary pulses into digits (with polling fallback if edge-detect is unavailable)
- `AudioPlayer`: single-thread WAV playback for tones/prompts
- `PhoneManager`: D-Bus bridge to `oFono` for dialing, answering, and hanging up calls
- `Telephone`: high-level behavior state machine (receiver up/down logic, ringing, shortcuts)

### Runtime behavior

- Handset lifted:
  - Starts `dial_tone.wav` (looped)
  - If an incoming call exists, answers automatically
  - Rotary digits are collected into a manual number
  - After a pause (~5s) with at least 3 digits, the number is dialed

- Handset down:
  - Active calls are hung up
  - Audio playback is stopped
  - One-digit rotary shortcuts are enabled:
    - `1..N`: dial corresponding entry in `phonebook.yaml`
    - `5`: ringer test
    - `9`: play turnoff prompt and shutdown the system

- Incoming calls:
  - If handset is down, the ringer pin pattern is used (1s ON / 4s OFF)
  - Lifting handset answers the incoming call

### Number handling

Before dialing, numbers are normalized to digits plus optional leading `+`.
If needed, fallback candidates are also tried (`+CC...`, `00CC...`, and national form)
to improve compatibility with modem/operator formatting expectations.

## `phonebook.yaml` format

The file is loaded relative to the script directory and should contain a YAML list:

```yaml
- name: Number 1
  number: xxxxxxx
- name: Number 2
  number: xxxxx
```

Notes:

- Keep `number` as a string (quotes are fine) if you want to preserve leading `+`.
- Shortcut `1` maps to first item, `2` to second, etc.

## Setup instructions

For detailed hardware and setup notes, see the <a href="https://hackaday.io/project/165208-an-old-rotary-phone-as-bluetooth-set" target="_blank">**hackaday.io page**</a> of the project.

The script resolves WAV prompts and `phonebook.yaml` relative to its own directory, so it can be cloned anywhere.
Run `telefonoa.py` with Python 3:

```bash
python3 telefonoa.py
```

Default GPIO assignment in the script:

- `HOERER_PIN = 13` (receiver/hook switch)
- `NS_PIN = 19` (rotary pulse input)
- Ringer output pin is fixed in code to GPIO `18`

If startup reports `org.freedesktop.DBus.Error.AccessDenied` for `org.ofono.Manager.GetModems`,
follow the steps in the **Small oFono configuration** section above.

If `oFono` is running but no modem is found, verify your modem stack and confirm `GetModems()` returns at least one modem.
