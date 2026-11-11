"""User configuration: app ID, preferred node and light names.

Stored as JSON at $ODELIC_CONFIG, or $XDG_CONFIG_HOME/odelic/config.json (default ~/.config/odelic/config.json):

    {"id": "12345678", "node": null, "idle_timeout": 60, "lights": {"living": "00000000"}}

idle_timeout: seconds of inactivity before the BLE link is released (so the phone app can connect);
0 keeps the link open permanently.
"""
import json
import os
from pathlib import Path

from . import protocol as P

ALL_NAMES = ("all", "*", "全部", "すべて")
DEFAULT_IDLE_TIMEOUT = 60.0
KEEP_WORDS = ("0", "never", "none", "off", "keep", "forever")


def parse_idle(value):
    """Seconds before an idle connection is closed. 0 (or 'never'/'keep') = stay connected."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().lower() in KEEP_WORDS:
        return 0.0
    v = float(value)
    if v < 0:
        raise ValueError("idle timeout must be >= 0")
    return v


def config_path() -> Path:
    if os.environ.get("ODELIC_CONFIG"):
        return Path(os.environ["ODELIC_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "odelic" / "config.json"


class Config:
    def __init__(self, data=None, path=None):
        data = data or {}
        self.path = Path(path) if path else config_path()
        self.id = data.get("id")
        self.node = data.get("node")
        self.lights = {str(k): str(v).lower() for k, v in (data.get("lights") or {}).items()}
        self.idle_timeout = parse_idle(data.get("idle_timeout"))

    @classmethod
    def load(cls, path=None):
        p = Path(path) if path else config_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        return cls(data, p)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"id": self.id, "node": self.node, "idle_timeout": self.idle_timeout, "lights": self.lights}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)      # the ID is the mesh password
        except OSError:
            pass
        tmp.replace(self.path)

    def app_id(self, override=None):
        return override or os.environ.get("ODELIC_ID") or self.id

    def idle(self, override=None):
        """Effective idle timeout: override > $ODELIC_IDLE_TIMEOUT > config > 60 s. 0 = stay connected."""
        for v in (override, os.environ.get("ODELIC_IDLE_TIMEOUT"), self.idle_timeout):
            v = parse_idle(v)
            if v is not None:
                return v
        return DEFAULT_IDLE_TIMEOUT

    # ------------------------------------------------------------ names
    def name_of(self, vaddr: bytes):
        for name, v in self.lights.items():
            if v == vaddr.hex():
                return name
        return None

    def resolve(self, target) -> bytes:
        """light name / 'all' / vaddr hex / bytes -> 4-byte vaddr"""
        if target is None:
            return P.BROADCAST
        if isinstance(target, (bytes, bytearray)):
            return P.vaddr(target)
        t = str(target).strip()
        if t.lower() in ALL_NAMES or t == "":
            return P.BROADCAST
        for name, v in self.lights.items():
            if name.lower() == t.lower():
                return bytes.fromhex(v)
        try:
            return P.vaddr(t)
        except ValueError:
            known = ", ".join(self.lights) or "(none)"
            raise ValueError("unknown light %r (known names: %s)" % (target, known)) from None

    def set_name(self, name, target):
        v = self.resolve(target)
        if v == P.BROADCAST:
            raise ValueError("cannot name the broadcast address")
        self.lights[name] = v.hex()

    def remove_name(self, name):
        if self.lights.pop(name, None) is None:
            raise KeyError(name)
