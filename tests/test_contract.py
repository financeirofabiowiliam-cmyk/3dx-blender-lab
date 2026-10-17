"""Offline GLB contract unit tests."""
from pathlib import Path
import json
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_glb import validate_file


class GLBValidatorTests(unittest.TestCase):
    def test_rejects_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "empty.glb"
            file.write_bytes(b"")
            with self.assertRaises(ValueError):
                validate_file(file)

    def test_accepts_minimal_skinned_glb(self):
        payload = json.dumps({"asset":{"version":"2.0"}, "meshes":[{}],
                              "skins":[{"joints":[0,1]}]}).encode()
        payload += b" " * (-len(payload) % 4)
        size = 20 + len(payload) + 8
        data = struct.pack("<4sII", b"glTF", 2, size)
        data += struct.pack("<I4s", len(payload), b"JSON") + payload
        data += struct.pack("<I4s", 0, b"BIN\x00")
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "minimal.glb"
            file.write_bytes(data)
            self.assertEqual(validate_file(file)["bones"], 2)


if __name__ == "__main__":
    unittest.main()
