"""Fixed C++-style wire vectors for SED v3; not an execution test of MCU C++."""
import base64
import hashlib
import json
from pathlib import Path
import unittest

from _load import protocol as p


class SedWireTests(unittest.TestCase):
    def setUp(self):
        self.vectors = json.loads((Path(__file__).parent / "fixtures/sed_v3.json").read_text(encoding="utf-8"))

    def test_raw_json_hmac_and_frozen_event_report(self):
        vector = self.vectors["state"]
        packet = vector["wire"].encode()
        message, signature, raw = p.decode(packet)
        self.assertTrue(p.authenticated(signature, raw, self.vectors["secret"]))
        self.assertEqual(message["values"], {"temperature": 0.0000125, "key_event": "single"})
        self.assertEqual(message["seq"], 42)
        self.assertEqual(message["report_id"], 7)
        self.assertFalse(p.authenticated(signature, raw.replace(b"single", b"hold__"), self.vectors["secret"]))

    def test_manifest_hash_uses_original_utf8_document(self):
        vector = self.vectors["manifest"]
        raw = vector["document"].encode()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), vector["sha256"])
        reconstructed = b"".join(base64.b64decode(chunk) for chunk in vector["chunks"])
        self.assertEqual(reconstructed, raw)
        self.assertEqual(p.manifest(raw)["name"], "门磁")

    def test_maximum_manifest_chunk_fits_framing(self):
        message = {"v": 3, "type": "manifest", "device": "a" * 16, "session": "b" * 16,
                   "backend_id": "c" * 32, "binding_id": "d" * 32, "ctx": "e" * 16,
                   "manifest_hash": "f" * 64, "part": 127, "parts": 128,
                   "payload": base64.b64encode(b"x" * 512).decode()}
        self.assertLessEqual(len(p.encode(message, self.vectors["secret"])), 1232)

    def test_unsigned_discovery_hello_is_v3(self):
        message, signature, _ = p.decode(self.vectors["hello"].encode())
        self.assertEqual(signature, "-")
        self.assertEqual(message["node_type"], "sed")
        self.assertTrue(p.hex_id(message["device"], 16))
        self.assertTrue(p.hex_id(message["request_id"], 16))
        self.assertEqual(message["generation"], 0)
        self.assertEqual(message["boot"], 1)
