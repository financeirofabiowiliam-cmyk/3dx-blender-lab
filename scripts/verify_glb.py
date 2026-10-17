"""Minimal dependency-free GLB export validator for synthetic development tests."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct


def validate_file(path: Path):
    data = path.read_bytes()
    if len(data) < 28:
        raise ValueError("Arquivo GLB vazio ou pequeno demais")
    magic, version, total = struct.unpack_from("<4sII", data, 0)
    if (magic, version, total) != (b"glTF", 2, len(data)):
        raise ValueError("Cabecalho GLB 2.0 incorreto")
    chunk_length, chunk_type = struct.unpack_from("<I4s", data, 12)
    if chunk_type != b"JSON" or not 4 <= chunk_length <= len(data)-28 or chunk_length % 4:
        raise ValueError("Chunk JSON GLB incorreto")
    metadata = json.loads(data[20:20+chunk_length].decode("utf-8"))
    if metadata.get("asset",{}).get("version") != "2.0":
        raise ValueError("Versao glTF incorreta")
    if not metadata.get("meshes"):
        raise ValueError("GLB sem malhas")
    if not metadata.get("skins"):
        raise ValueError("GLB sem skin/armature")
    return {
        "schema":"3dx-glb-validate/1",
        "valid":True,
        "meshes":len(metadata["meshes"]),
        "bones":sum(len(x.get("joints",[])) for x in metadata["skins"]),
        "animations":len(metadata.get("animations",[])),
        "bytes":len(data),
        "sha256":hashlib.sha256(data).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = validate_file(args.file)
    if args.report:
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
