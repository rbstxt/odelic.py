"""Packet builders/parsers for ODELIC lights on the Pairlink mesh.

Mirrors jp.co.odelic.smt.remote03.act.module.ControllerAct in "central" (Tiger) mode:
    03 <dst vaddr 4B> <channel> <own vaddr 4B> <msg id> <args...>
"""
from dataclasses import dataclass

BROADCAST = b"\xff\xff\xff\xff"

# channels
CH_TO_LIGHT = 0x20
CH_TO_LIGHT_BROADCAST = 0x2A
CH_PING = 0xFE
CH_PING_RESPONSE = 0xFF

# message ids (MeshProfile.DATAEVENT_MSGID_*)
MSG_LIGHT = 0xC0          # SM_BRIGHT_LIGHT
MSG_LIGHT_GROUP = 0xC1    # SM_BRIGHT_LIGHT_GROUP
MSG_CHANGE_COLOR = 0xE2   # SM_CHANGE_COLOR (cycle colour preset)
MSG_SCENE_SAVE = 0x40     # SM_SCENE_IN
MSG_SCENE_PLAY = 0x42     # SM_SCENE_PLAY
MSG_STATUS = 0x70         # SM_STATUS

# special (x, y) pairs
XY_ON = (55, 55)
XY_OFF = (50, 50)

X_MAX = 20   # colour: 0 = warm (電球色) ... 20 = cool (昼光色)
Y_MAX = 19   # brightness: 0 = brightest ... 19 = dimmest


def color_to_x(warmth_cool: int) -> int:
    """0 (warmest) .. 100 (coolest) -> x 0..20"""
    return max(0, min(X_MAX, round(warmth_cool * X_MAX / 100)))


def brightness_to_y(brightness: int) -> int:
    """1..100 % -> y 19..0 (same rule as the app: (100 - b) / 5, clamped)"""
    return max(0, min(Y_MAX, (100 - brightness) // 5))


def vaddr(v) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
    elif isinstance(v, int):
        b = v.to_bytes(4, "little")
    else:
        b = bytes.fromhex(str(v).replace(":", ""))
    if len(b) != 4:
        raise ValueError("vaddr must be 4 bytes")
    return b


def data_event(dst: bytes, channel: int, payload: bytes) -> bytes:
    return bytes([0x03]) + dst + bytes([channel]) + payload


def light(own: bytes, dst: bytes, x: int, y: int, index: int = 0) -> bytes:
    """ControllerAct.setlight: on/off/colour/brightness for one light or all (broadcast)."""
    ch = CH_TO_LIGHT_BROADCAST if dst == BROADCAST else CH_TO_LIGHT
    return data_event(dst, ch, own + bytes([MSG_LIGHT, index, x, y, 0, 0, 0, 0]))


def group_light(own: bytes, group_id: int, x: int, y: int) -> bytes:
    """ControllerAct.sendgroup_new"""
    return data_event(BROADCAST, CH_TO_LIGHT, own + bytes([MSG_LIGHT_GROUP, x, y, 0, 0, 0, 0, 0, 0, group_id]))


def change_color(own: bytes, dst: bytes) -> bytes:
    return data_event(dst, CH_TO_LIGHT, own + bytes([MSG_CHANGE_COLOR]))


def scene(own: bytes, index: int, save: bool, dst: bytes = BROADCAST) -> bytes:
    msg = MSG_SCENE_SAVE if save else MSG_SCENE_PLAY
    return data_event(dst, CH_TO_LIGHT, own + bytes([msg, 0, 0, 0, 0, 0, 0, index]))


def status_request(own: bytes, dst: bytes) -> bytes:
    return data_event(dst, CH_TO_LIGHT, own + bytes([MSG_STATUS]))


def ping_all(own: bytes) -> bytes:
    return data_event(BROADCAST, CH_PING, own)


@dataclass
class Device:
    vaddr: bytes
    mac: str
    product: int
    version: str

    def __str__(self):
        return "vaddr=%s mac=%s product=%d ver=%s" % (self.vaddr.hex(), self.mac, self.product, self.version)


def parse_ping_response(p: bytes):
    """MeshCommon.GetPingResponse; p is a decrypted type-3 packet with channel 0xFF."""
    if len(p) < 20 or p[0] != 0x03 or p[5] != CH_PING_RESPONSE:
        return None
    mac = ":".join("%02X" % b for b in reversed(p[6:12]))
    product = p[16] | (p[17] << 8)
    return Device(vaddr=p[12:16], mac=mac, product=product, version="%d.%d.%d" % (product, p[18], p[19]))


MSG_STATUS_RESPONSES = (0x71, 0x35)   # STATUS_MAIN / STATE_RESPONSE_FD


@dataclass
class LightState:
    vaddr: bytes
    on: bool
    x: int
    y: int

    @property
    def color(self):
        """0 = warm .. 100 = cool, None if unknown (light reports a plain 'on')."""
        return None if not self.on or self.x > X_MAX else self.x * 5

    @property
    def brightness(self):
        """1..100 %, 0 when off, None if unknown."""
        if not self.on:
            return 0
        return None if self.y > Y_MAX else 100 - self.y * 5

    def as_dict(self):
        return {"vaddr": self.vaddr.hex(), "on": self.on, "color": self.color,
                "brightness": self.brightness, "x": self.x, "y": self.y}


def parse_status(p: bytes):
    """Status reply (MeshBlePresenter.syncState): 03 <dst4> <ch> <src4> 71 <x> <y> ..."""
    if len(p) < 13 or p[0] != 0x03 or p[10] not in MSG_STATUS_RESPONSES:
        return None
    x, y = p[11], p[12]
    return LightState(vaddr=p[6:10], on=(x, y) != XY_OFF, x=x, y=y)


def parse_app_id(app_id: str):
    """The app shows 'ID:<HomeID><4-char password>', e.g. 'ID:12345678' -> (1234, '5678')."""
    s = app_id.strip()
    if s.upper().startswith("ID:"):
        s = s[3:]
    if len(s) < 5:
        raise ValueError("ID looks too short: %r" % app_id)
    return int(s[:-4]), s[-4:]


def adv_homeid(manufacturer_data: dict):
    """HomeID advertised by a mesh node (manufacturer id 0xFFFF, c0/c1 ff 01|02 <homeid LE>)."""
    v = manufacturer_data.get(0xFFFF)
    if v and len(v) >= 7 and v[0] in (0xC0, 0xC1) and v[1] == 0xFF and v[2] in (1, 2):
        return int.from_bytes(v[3:7], "little")
    return None
