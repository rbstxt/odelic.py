"""Offline tests.

The mesh vectors below were produced by the original native library from the official app
(run under tools/native_oracle.py) for a made-up mesh: HomeID 1234 / password "5678".
"""
import unittest

from odelic import protocol as P
from odelic.crypto import AES128, LoginError, MeshCrypto

OWN = bytes.fromhex("02000000")


class TestAES(unittest.TestCase):
    def test_fips197_vector(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        pt = bytes.fromhex("00112233445566778899aabbccddeeff")
        ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(AES128(key).encrypt(pt), ct)
        self.assertEqual(AES128(key).decrypt(ct), pt)


class TestMesh(unittest.TestCase):
    def setUp(self):
        self.m = MeshCrypto(1234, "5678")

    def login(self):
        self.m.answer_challenge(bytes.fromhex("10d9e9ae24df368efd29dce10311fcd5"))

    def test_login_answers(self):
        cases = {
            "10d9e9ae24df368efd29dce10311fcd5": "0116033f7fc1c4a4c20ab25e3214b34fc94325",
            "4cfd0d8ba5c948bab1cd0b120171ffdd": "011603aa1dba22c58d8894f691dd26c2d6fe99",
        }
        for ch, ans in cases.items():
            self.assertEqual(self.m.answer_challenge(bytes.fromhex(ch)).hex(), ans)

    def test_wrong_homeid(self):
        with self.assertRaises(LoginError):
            MeshCrypto(1111, "9999").answer_challenge(bytes.fromhex("10d9e9ae24df368efd29dce10311fcd5"))

    def test_light_packets(self):
        self.login()
        cases = {
            P.XY_OFF: "06ffffffff2a5f2cc7de934ec8bfa57cd54edc21093a",
            (0, 0): "06ffffffff2af540faabb918376c638574d9dcc467f9",  # warm, brightest
        }
        for xy, expected in cases.items():
            plain = P.light(OWN, P.BROADCAST, *xy)
            enc = self.m.encrypt(plain)
            self.assertEqual(enc.hex(), expected)
            self.assertEqual(self.m.decrypt(enc), plain)

    def test_helpers(self):
        self.assertEqual(P.parse_app_id("ID:12345678"), (1234, "5678"))
        self.assertEqual(P.brightness_to_y(100), 0)
        self.assertEqual(P.brightness_to_y(1), 19)
        self.assertEqual(P.color_to_x(0), 0)
        self.assertEqual(P.color_to_x(100), 20)


if __name__ == "__main__":
    unittest.main()
