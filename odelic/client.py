"""BLE client for ODELIC lights (bleak)."""
import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner

from . import protocol as P
from .crypto import LoginError, MeshCrypto

log = logging.getLogger("odelic")

UUID_WRITE = "9e5d1e47-5c13-43a0-8635-82adffc1386f"
UUID_NOTIFY = "9e5d1e47-5c13-43a0-8635-82adffc2386f"


async def find_nodes(homeid=None, timeout=8.0):
    """Scan for mesh nodes. Returns [(BLEDevice, homeid, rssi)] sorted by signal strength."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for dev, adv in found.values():
        hid = P.adv_homeid(adv.manufacturer_data)
        if hid is not None and (homeid is None or hid == homeid):
            out.append((dev, hid, adv.rssi))
    return sorted(out, key=lambda t: -t[2])


class OdelicMesh:
    """One logged-in connection to an ODELIC mesh.

        async with OdelicMesh("12345678") as mesh:
            await mesh.set_light(color=0, brightness=100)   # warm, full
            await mesh.off()
    """

    def __init__(self, app_id=None, *, homeid=None, password=None, device=None, scan_timeout=8.0):
        if app_id is not None:
            homeid, password = P.parse_app_id(app_id)
        if homeid is None or password is None:
            raise ValueError("give app_id (e.g. '12345678') or homeid + password")
        self.homeid = homeid
        self.crypto = MeshCrypto(homeid, password)
        self.device = device            # BLEDevice / address / name, or None = strongest node
        self.scan_timeout = scan_timeout
        self.own = None                 # our vaddr in the mesh (assigned at login)
        self.node_name = None
        self._client = None
        self._rx = None
        self._logging_in = False
        self._seg = bytearray()
        self._seg_seq = 0
        self._listeners = []
        self.last_rx = 0.0              # monotonic time of the last packet we could decrypt
        self.suspect = False            # True when the session may be out of sync with the node

    # ------------------------------------------------------------ connection
    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    async def _resolve_device(self):
        if self.device is not None and not isinstance(self.device, str):
            return self.device
        nodes = await find_nodes(self.homeid, self.scan_timeout)
        if isinstance(self.device, str):
            nodes = [n for n in nodes if self.device in (n[0].name, n[0].address)]
        if not nodes:
            raise ConnectionError("no mesh node with HomeID %d found" % self.homeid)
        log.info("using node %s (%s) rssi %d", nodes[0][0].name, nodes[0][0].address, nodes[0][2])
        return nodes[0][0]

    async def connect(self, timeout=10.0):
        dev = await self._resolve_device()
        self.node_name = getattr(dev, "name", None) or str(dev)
        self.own = None
        self._rx = asyncio.Queue()
        self._logging_in = True
        self._client = BleakClient(dev, disconnected_callback=self._on_disconnect)
        await self._client.connect()
        try:
            await self._client.start_notify(UUID_NOTIFY, self._on_notify)
            await self._write(bytes([0x01, 0x20]))          # ask for login challenge
            await asyncio.wait_for(self._login(), timeout)
        except BaseException:
            await self.disconnect()
            raise
        finally:
            self._logging_in = False
        self.last_rx = time.monotonic()
        self.suspect = False
        log.info("logged in via %s (own vaddr %s)", self.node_name, self.own.hex())

    async def resync(self, timeout=8.0):
        """Re-run the login handshake on the existing link to pick up the node's current session.

        The node answers 08 ("already logged in") but still switches to the new challenge's
        session, which we answer too, so commands are understood again afterwards.
        A status of 00 means the node forgot us (reboot); wait for the new vaddr then.
        """
        if self._client is None or not self._client.is_connected:
            raise LoginError("not connected")
        old_own = self.own
        self._rx = asyncio.Queue()
        self.own = None
        self._logging_in = True
        try:
            await self._write(bytes([0x01, 0x20]))
            await asyncio.wait_for(self._login(resync=True, old_own=old_own), timeout)
        except BaseException:
            if self.own is None:
                self.own = old_own
            raise
        finally:
            self._logging_in = False
        self.suspect = False
        log.warning("session resynchronised with %s (vaddr %s)",
                    self.node_name, self.own.hex() if self.own else "?")

    async def ensure_live(self, timeout=8.0):
        """Make sure the link can talk to the node; resync/reconnect when it looks stale."""
        if self._client is None or not self._client.is_connected or self.own is None:
            await self.disconnect()
            await self.connect()
            return
        if self.suspect:
            try:
                await self.resync(timeout)
            except BaseException:
                await self.disconnect()
                await self.connect()

    @property
    def is_connected(self):
        return self._client is not None and self._client.is_connected and self.own is not None

    def _on_disconnect(self, _client):
        log.warning("node %s disconnected", self.node_name)
        self.own = None

    async def disconnect(self):
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None
                self.own = None

    async def _login(self, resync=False, old_own=None):
        answered = False
        while True:
            p = await self._rx.get()
            if p[:2] == b"\x02\x20" and len(p) >= 18:
                await self._write(self.crypto.answer_challenge(p[2:18]))
                answered = True
            elif p[:2] == b"\x02\x16" and answered:
                status = p[2] if len(p) >= 3 else None
                if resync and status == 0x08:
                    if self.own is None:
                        self.own = old_own  # 08 keeps our existing login; no new vaddr is sent
                    return
                if status == 0x08:
                    raise LoginError("node is already in use by another controller "
                                     "(close the ODELIC app / turn off phone Bluetooth and retry)")
                if status != 0:
                    raise LoginError("login rejected (status %s)" % p[2:].hex())
                if resync:
                    if self.own is not None and self.own != old_own:
                        return  # 00 with a fresh vaddr arrived via _on_notify
                    continue    # 00: wait for the new 01 0a vaddr assignment
            if not resync and self.own is not None:
                return
            if resync and self.own is not None and self.own != old_own:
                return

    # ------------------------------------------------------------ transport
    async def _write(self, data):
        log.debug("-> %s", data.hex())
        await self._client.write_gatt_char(UUID_WRITE, data, response=False)

    def _on_notify(self, _, data):
        data = bytes(data)
        log.debug("<- %s", data.hex())
        if len(data) >= 3 and data[0] == 4 and data[1] == 4:          # segmented PDU
            seq, total = data[2] & 0x0F, data[2] >> 4
            if seq != self._seg_seq + 1:
                self._seg, self._seg_seq = bytearray(), 0
                return
            self._seg += data[3:]
            self._seg_seq = seq
            if seq != total:
                return
            data, self._seg, self._seg_seq = bytes(self._seg), bytearray(), 0
        try:
            plain = self.crypto.decrypt(data)
        except ValueError:
            log.debug("undecryptable packet %s", data.hex())
            if self.own is not None and not self._logging_in:
                self.suspect = True           # node encrypts with a session we do not have
            return
        if plain is not data and plain[:1] == b"\x03":
            self.last_rx = time.monotonic()
        data = plain
        if data[:2] == b"\x01\x0a" and len(data) >= 6:
            self.own = data[2:6]
        if self._logging_in:
            self._rx.put_nowait(data)
        elif data[:2] == b"\x02\x20" and len(data) >= 18 and self.own is not None:
            # someone else (another process on this Mac shares the link) started a login:
            # the node now uses that challenge's session, so follow it
            if self.crypto.adopt_challenge(data[2:18]):
                self.suspect = False
                log.warning("another controller started a login on this link; following its session")
        for cb in list(self._listeners):
            cb(data)

    async def send(self, packet: bytes):
        """Encrypt and send a type-3 data-event packet (see odelic.protocol)."""
        if self.own is None:
            raise LoginError("not connected")
        enc = self.crypto.encrypt(packet)
        mtu = max(20, self._client.mtu_size - 3)
        if len(enc) <= mtu:
            return await self._write(enc)
        chunk = mtu - 3
        total = -(-len(enc) // chunk)
        for i in range(total):
            await self._write(bytes([0x01, 0x21, (total << 4) | (i + 1)]) + enc[i * chunk:(i + 1) * chunk])
            await asyncio.sleep(0.015)

    def add_listener(self, callback):
        """callback(packet: bytes) for every received (decrypted) packet."""
        self._listeners.append(callback)

    # ------------------------------------------------------------ actions
    async def on(self, target=P.BROADCAST):
        """Turn on (restores the previous colour/brightness)."""
        await self.send(P.light(self.own, P.vaddr(target), *P.XY_ON))

    async def off(self, target=P.BROADCAST):
        await self.send(P.light(self.own, P.vaddr(target), *P.XY_OFF))

    async def set_light(self, color=None, brightness=100, target=P.BROADCAST, *, x=None, y=None):
        """color: 0 = warm (電球色) .. 100 = cool (昼光色); brightness: 1..100 %.
        Or pass raw x (0..20) / y (0..19, 0 = brightest)."""
        x = P.color_to_x(50 if color is None else color) if x is None else x
        y = P.brightness_to_y(brightness) if y is None else y
        await self.send(P.light(self.own, P.vaddr(target), x, y))

    async def night_light(self, level=0, target=P.BROADCAST):
        """Dim warm night light, level 0..2 (what the app does for plain lights)."""
        await self.send(P.light(self.own, P.vaddr(target), 0, 17 + max(0, min(2, level))))

    async def set_group(self, group_id, color=None, brightness=100, *, x=None, y=None, on=None):
        """Same as set_light but for a group id (as configured in the app). on=True/False for on/off."""
        if on is not None:
            x, y = P.XY_ON if on else P.XY_OFF
        x = P.color_to_x(50 if color is None else color) if x is None else x
        y = P.brightness_to_y(brightness) if y is None else y
        await self.send(P.group_light(self.own, group_id, x, y))

    async def change_color(self, target):
        """Cycle the colour preset of one light (light_change_color)."""
        await self.send(P.change_color(self.own, P.vaddr(target)))

    async def save_scene(self, index):
        await self.send(P.scene(self.own, index, save=True))

    async def play_scene(self, index):
        await self.send(P.scene(self.own, index, save=False))

    async def devices(self, timeout=3.0):
        """Ping the whole mesh and return the responding lights."""
        found = {}

        def cb(p):
            d = P.parse_ping_response(p)
            if d:
                found[d.vaddr] = d

        self._listeners.append(cb)
        try:
            await self.send(P.ping_all(self.own))
            await asyncio.sleep(timeout)
        finally:
            self._listeners.remove(cb)
        return list(found.values())

    async def status(self, target, timeout=3.0):
        """Ask one light (vaddr) for its state. Returns protocol.LightState, or None on timeout."""
        dst = P.vaddr(target)
        fut = asyncio.get_running_loop().create_future()

        def cb(p):
            st = P.parse_status(p)
            if st and st.vaddr == dst and not fut.done():
                fut.set_result(st)

        self._listeners.append(cb)
        try:
            await self.send(P.status_request(self.own, dst))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._listeners.remove(cb)
