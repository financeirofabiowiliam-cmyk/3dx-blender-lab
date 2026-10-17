"""Durable local 3DX production queue, isolated from the official game.

One worker thread launches allowlisted Blender scripts in background processes.
SQLite and job directories are kept on THREEDX_DATA_DIR; deploy with a persistent
disk to retain results across machine replacement. There is no neural fallback.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
import warnings
import zipfile

MAX_REFERENCE_BYTES = 4 * 1024 * 1024
MAX_REFERENCES = 8
MAX_MOCAP_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 48 * 1024 * 1024
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_PENDING = 32
PARAMETER_RANGES = {
    "height": (1.2, 2.3), "head_scale": (.85, 1.3),
    "shoulder_width": (.75, 1.25), "waist_width": (.75, 1.3),
    "hip_width": (.8, 1.25), "arm_length": (.85, 1.15),
    "leg_length": (.85, 1.15), "body_depth": (.82, 1.18),
}
DEFAULT_PARAMETERS = {key: (1.8 if key == "height" else 1.0) for key in PARAMETER_RANGES}
STYLE_DEFAULTS = {
    "anime": {"head_scale": 1.12, "shoulder_width": .92, "waist_width": .90},
    "semi_realistic": {"head_scale": 1.02, "shoulder_width": 1.0, "waist_width": .96},
    "realistic": {"head_scale": 1.0, "shoulder_width": .96, "waist_width": 1.02},
}
BODY_TYPES = {
    "natural": {},
    "slim": {"shoulder_width": .88, "waist_width": .82, "hip_width": .91, "leg_length": 1.04},
    "athletic": {"shoulder_width": 1.12, "waist_width": .88, "hip_width": .97, "arm_length": 1.03},
    "curvy": {"shoulder_width": .98, "waist_width": .84, "hip_width": 1.18},
    "heroic": {"head_scale": .93, "shoulder_width": 1.23, "waist_width": .85, "hip_width": 1.05, "leg_length": 1.08},
    "robust": {"shoulder_width": 1.19, "waist_width": 1.20, "hip_width": 1.19, "leg_length": .98},
    "anime": {"head_scale": 1.20, "shoulder_width": .91, "waist_width": .87, "hip_width": 1.02, "leg_length": 1.08},
    "custom": {},
}
MIMES = {".glb": "model/gltf-binary", ".blend": "application/octet-stream",
         ".json": "application/json", ".log": "text/plain; charset=utf-8",
         ".zip": "application/zip", ".png": "image/png"}
ID_RE = re.compile(r"^[a-f0-9]{32}$")


class ProductionError(ValueError):
    def __init__(self, message, code="invalid_request", status=400):
        super().__init__(message)
        self.code, self.status = code, status


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def emit(event, **fields):
    print(json.dumps({"event": event, "time": now(), **fields}, ensure_ascii=False), flush=True)


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def pipeline_directory(base):
    """Support both the repository and the existing Render Docker layout."""
    candidates = [Path(base).parent / "pipeline", Path(base) / "tools/3dx/pipeline"]
    return next((p for p in candidates if p.is_dir()), candidates[0])


def load_module(path, name):
    import sys
    # Sibling imports are intentional project-owned modules, never upload paths.
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_config(value):
    if not isinstance(value, dict):
        raise ProductionError("Expected a JSON object.")
    allowed = {"name", "style", "quality", "parameters", "rig", "animations", "outfit", "references", "mocap", "body_type", "motion_pack", "garment_slots", "appearance_mode", "rig_profile", "preprocess_device"}
    if set(value) - allowed:
        raise ProductionError("Unknown configuration field: " + sorted(set(value) - allowed)[0])
    name = value.get("name", "Urban Black — test character")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
        raise ProductionError("Character name must contain 1 to 80 printable characters.")
    config = {"name": name.strip(), "style": value.get("style", "semi_realistic"),
              "quality": value.get("quality", "standard"), "outfit": value.get("outfit", "urban_black"),
              "rig": value.get("rig", True), "animations": value.get("animations", True),
              "body_type": value.get("body_type", "natural"),
              "motion_pack": value.get("motion_pack", "procedural"),
              "rig_profile": value.get("rig_profile", "standard"),
              "appearance_mode": value.get("appearance_mode", "preset"),
              "preprocess_device": value.get("preprocess_device", "server")}
    for key, choices in (("style", ("anime", "semi_realistic", "realistic")),
                         ("quality", ("preview", "standard", "high")),
                         ("outfit", ("urban_black", "basic", "streetwear", "sport", "formal", "none"))):
        if config[key] not in choices:
            raise ProductionError("Invalid " + key + ".")
    if type(config["rig_profile"]) is not str or config["rig_profile"] not in ("standard", "detail_v1"):
        raise ProductionError("Invalid rig_profile.")
    if config["rig_profile"] == "detail_v1" and config["rig"] is not True:
        raise ProductionError("Advanced rig requires a skinned humanoid.")
    if config["preprocess_device"] not in ("server", "windows", "android", "both"):
        raise ProductionError("Invalid preprocess_device.")
    if config["appearance_mode"] not in ("preset", "reference_skin"):
        raise ProductionError("Invalid appearance_mode.")
    if config["motion_pack"] not in ("procedural", "quaternius_cc0"):
        raise ProductionError("Invalid motion_pack.")
    if config["motion_pack"] != "procedural" and not (config["rig"] and config["animations"]):
        raise ProductionError("The licensed motion library requires an animated humanoid rig.")
    available_slots = ("top", "bottom", "footwear", "headwear", "accessory")
    slots = value.get("garment_slots", list(available_slots))
    if (not isinstance(slots, list) or len(slots) > len(available_slots)
            or any(type(slot) is not str or slot not in available_slots for slot in slots)
            or len(slots) != len(set(slots))):
        raise ProductionError("Invalid garment_slots.")
    config["garment_slots"] = slots
    if not isinstance(config["body_type"], str) or config["body_type"] not in BODY_TYPES:
        raise ProductionError("Invalid body_type.")
    if type(config["rig"]) is not bool or type(config["animations"]) is not bool:
        raise ProductionError("rig and animations must be booleans.")
    if config["animations"] and not config["rig"]:
        raise ProductionError("Animations require a rig.")
    parameters = value.get("parameters", {})
    if not isinstance(parameters, dict) or set(parameters) - set(PARAMETER_RANGES):
        raise ProductionError("Unknown anatomy parameter.")
    config["parameters"] = {**DEFAULT_PARAMETERS, **STYLE_DEFAULTS[config["style"]], **BODY_TYPES[config["body_type"]]}
    for key, number in parameters.items():
        low, high = PARAMETER_RANGES[key]
        if type(number) not in (int, float) or not low <= number <= high:
            raise ProductionError(f"{key} must be between {low} and {high}.")
        config["parameters"][key] = float(number)
    references = value.get("references", [])
    if not isinstance(references, list) or len(references) > MAX_REFERENCES:
        raise ProductionError(f"At most {MAX_REFERENCES} reference images are supported.")
    seen = set()
    # Opt-in pose metadata is always validated; the generator can ignore it.
    pose_validator = None
    for ref in references:
        if not isinstance(ref, dict) or set(ref) - {"view", "name", "data", "pose"}:
            raise ProductionError("Each reference must contain view, name and base64 data.")
        view = ref.get("view")
        if view not in ("front", "back", "left", "right", "additional"):
            raise ProductionError("Invalid reference view.")
        if view != "additional" and view in seen:
            raise ProductionError("Duplicate primary reference view: " + view)
        seen.add(view)
        if "pose" in ref:
            if pose_validator is None:
                pose_validator = load_module(pipeline_directory(Path(__file__).resolve().parent) /
                                             "pose_landmark_fit.py", "threedx_pose_validator").validate_pose
            try:
                ref["pose"] = pose_validator(ref["pose"])
            except (TypeError, ValueError) as exc:
                raise ProductionError("Invalid optional body landmark observation.", "invalid_pose", 422) from exc
        if not isinstance(ref.get("data"), str) or len(ref["data"]) > ((MAX_REFERENCE_BYTES + 2) // 3) * 4:
            raise ProductionError("Each reference must be a PNG/JPEG of at most 4 MiB.", "image_too_large", 413)
        filename = ref.get("name", "reference")
        if not isinstance(filename, str) or len(filename) > 160 or any(ord(c) < 32 for c in filename):
            raise ProductionError("Invalid reference name.")
    mocap = value.get("mocap")
    if mocap is not None:
        if not config["animations"]:
            raise ProductionError("Motion Capture requires an animated rig.")
        if not isinstance(mocap, dict) or set(mocap) - {"name", "data"}:
            raise ProductionError("Motion Capture must contain name and BVH data.")
        if not isinstance(mocap.get("data"), str) or not isinstance(mocap.get("name"), str):
            raise ProductionError("BVH requires base64 data and a file name.")
        if not 1 <= len(mocap["name"]) <= 160 or any(ord(c) < 32 for c in mocap["name"]):
            raise ProductionError("Invalid BVH file name.")
        if len(mocap["data"]) > ((MAX_MOCAP_BYTES + 2) // 3) * 4:
            raise ProductionError("BVH exceeds 1 MiB.", "mocap_too_large", 413)
        config["mocap"] = mocap
    return config, references


def normalize_mocap(value, directory):
    """Constrain BVH complexity before any native Blender parsing."""
    try:
        raw = base64.b64decode(value["data"], validate=True)
        data = raw.decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ProductionError("Motion Capture must be UTF-8 BVH.", "invalid_mocap", 422) from exc
    if not raw or len(raw) > MAX_MOCAP_BYTES or "\x00" in data:
        raise ProductionError("BVH is empty, binary, or too large.", "invalid_mocap", 422)
    if not data.lstrip().startswith("HIERARCHY") or "MOTION" not in data:
        raise ProductionError("BVH hierarchy or motion section missing.", "invalid_mocap", 422)
    count = re.search(r"(?m)^\s*Frames:\s*(\d+)\s*$", data)
    timing = re.search(r"(?m)^\s*Frame Time:\s*([0-9.]+)\s*$", data)
    frames = int(count.group(1)) if count else 0
    dt = float(timing.group(1)) if timing else 0.0
    joints = len(re.findall(r"(?m)^\s*(?:ROOT|JOINT)\s+", data))
    if not (4 <= frames <= 240 and 0.004 <= dt <= 0.2 and
            8 <= joints <= 100 and data.count("CHANNELS") <= 100):
        raise ProductionError("BVH exceeds supported frame/joint limits or is malformed.",
                              "invalid_mocap", 422)
    if any(ord(c) < 32 and c not in "\n\r\t" for c in data):
        raise ProductionError("BVH contains invalid control characters.", "invalid_mocap", 422)
    filename = "motion-capture.bvh"
    (directory / filename).write_bytes(raw)
    return {"file": filename, "name": value["name"], "frames": frames,
            "joints": joints, "bytes": len(raw)}


def normalize_reference(ref, directory, index):
    """Decode, verify and re-encode images. Never trust user filenames or metadata."""
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise ProductionError("Pillow is required for image uploads.", "image_processor_unavailable", 503) from exc
    try:
        data = base64.b64decode(ref["data"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProductionError("Invalid base64 reference image.") from exc
    if not data or len(data) > MAX_REFERENCE_BYTES:
        raise ProductionError("Each reference must be a PNG/JPEG of at most 4 MiB.", "image_too_large", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ("PNG", "JPEG"):
                    raise ProductionError("Only PNG and JPEG references are supported.")
                if image.width * image.height > MAX_IMAGE_PIXELS or min(image.size) < 32:
                    raise ProductionError("Reference dimensions must be at least 32 px and at most 16 megapixels.")
                if getattr(image, "n_frames", 1) != 1:
                    raise ProductionError("Animated reference images are not supported.")
                image.verify()
            with Image.open(io.BytesIO(data)) as source:
                # Reduce pixel count before expanding into four-channel RGBA.
                # The silhouette estimator subsequently samples at <=384px.
                source.draft("RGB", (1024, 1024))
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((1024, 1024))
                image = oriented.convert("RGBA")
                filename = f"reference-{index:02d}-{ref['view']}.png"
                target = directory / filename
                image.save(target, format="PNG")
                width, height = image.size
    except ProductionError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ProductionError("Reference image could not be decoded safely.") from exc
    normalized = {"view": ref["view"], "name": ref.get("name", "reference"), "file": filename,
                  "width": width, "height": height, "bytes": target.stat().st_size}
    if "pose" in ref:
        normalized["pose"] = ref["pose"]  # Already strict-validated before image decoding.
    return normalized


class JobStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "jobs.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, character_id TEXT NOT NULL, version INTEGER NOT NULL,
                    name TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0, config TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    error TEXT, result TEXT, artifacts TEXT NOT NULL DEFAULT '[]',
                    parent_id TEXT, attempt INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(character_id, version));
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def directory(self, job_id):
        if not isinstance(job_id, str) or not ID_RE.fullmatch(job_id):
            raise ProductionError("Unknown job.", "not_found", 404)
        return self.root / job_id

    def get_internal(self, job_id):
        self.directory(job_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ProductionError("Unknown job.", "not_found", 404)
        return dict(row)

    def public(self, row, include_events=False):
        job = dict(row)
        config = json.loads(job["config"])
        config["references"] = [{k: v for k, v in ref.items() if k != "file"} for ref in config.get("references", [])]
        if isinstance(config.get("mocap"), dict):
            config["mocap"] = {k: v for k, v in config["mocap"].items() if k != "file"}
        job["config"] = config
        for key in ("error", "result", "artifacts"):
            job[key] = json.loads(job[key]) if job[key] else None
        if include_events:
            with self.connect() as db:
                job["events"] = [dict(row) for row in db.execute(
                    "SELECT created_at, stage, message FROM events WHERE job_id=? ORDER BY sequence", (job["id"],))]
        return job

    def get(self, job_id):
        return self.public(self.get_internal(job_id), include_events=True)

    def list(self, limit=100):
        with self.connect() as db:
            return [self.public(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),))]

    def catalog(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status='succeeded' ORDER BY created_at DESC LIMIT 500").fetchall()
        characters = {}
        for row in rows:
            job = self.public(row)
            # A result removed externally is not advertised as an available character.
            if not (self.directory(job["id"]) / "character.glb").is_file():
                continue
            character = characters.setdefault(job["character_id"], {
                "id": job["character_id"], "name": job["name"], "latest": job, "versions": []})
            character["versions"].append(job)
        return list(characters.values())

    def create(self, value, parent_id=None):
        config, references = validate_config(value)
        parent = self.get_internal(parent_id) if parent_id else None
        if parent and parent["status"] in ("queued", "running", "awaiting_local", "local_processing"):
            raise ProductionError("Wait for the current job to finish before retrying.", "job_active", 409)
        job_id = uuid.uuid4().hex
        directory = self.directory(job_id)
        directory.mkdir(mode=0o700)
        try:
            if parent and "references" not in value:
                old_config = json.loads(parent["config"])
                config["references"] = old_config.get("references", [])
                for ref in config["references"]:
                    shutil.copyfile(self.directory(parent_id) / ref["file"], directory / ref["file"])
            else:
                config["references"] = [normalize_reference(ref, directory, i) for i, ref in enumerate(references)]
            if config["appearance_mode"] == "reference_skin":
                views = {ref["view"] for ref in config["references"]}
                if not ({"front", "back"} <= views and ("left" in views or "right" in views)):
                    raise ProductionError(
                        "Skin própria requer três imagens: frente, lateral e costas.",
                        "insufficient_reference_views", 422)
            if parent and "mocap" not in value:
                old = json.loads(parent["config"]).get("mocap")
                if old:
                    config["mocap"] = old
                    shutil.copyfile(self.directory(parent_id) / old["file"],
                                    directory / "motion-capture.bvh")
            elif config.get("mocap"):
                config["mocap"] = normalize_mocap(config["mocap"], directory)
            character_id = parent["character_id"] if parent else uuid.uuid4().hex
            stamp = now()
            local = config["preprocess_device"] in ("windows", "android", "both") and bool(config["references"])
            initial = "awaiting_local" if local else "queued"
            first_stage = "awaiting_android" if config["preprocess_device"] == "both" and local else initial
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                pending = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','awaiting_local','local_processing')").fetchone()[0]
                if pending >= MAX_PENDING:
                    raise ProductionError("Production queue is full; retry after a task completes.", "queue_full", 429)
                version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM jobs WHERE character_id=?", (character_id,)).fetchone()[0]
                db.execute("""INSERT INTO jobs (id, character_id, version, name, status, stage,
                           config, created_at, updated_at, parent_id, attempt)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                           (job_id, character_id, version, config["name"], initial, first_stage,
                            json.dumps(config, allow_nan=False), stamp, stamp, parent_id,
                            parent["attempt"] + 1 if parent else 1))
                db.execute("INSERT INTO events(job_id,created_at,stage,message) VALUES (?,?,?,?)",
                           (job_id, stamp, first_stage,
                             "Android and Windows scheduled together; each stage has Render fallback."
                             if config["preprocess_device"] == "both" and local else
                             "Waiting for optional device helper with automatic Render fallback."
                             if local else "Task persisted and queued."))
            emit("job_queued", job_id=job_id, version=version)
            return self.get(job_id)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def retry(self, job_id, overrides):
        if not isinstance(overrides, dict):
            raise ProductionError("Expected retry overrides as a JSON object.")
        parent = self.get_internal(job_id)
        config = json.loads(parent["config"])
        config.pop("references", None)
        config.pop("mocap", None)
        if "style" in overrides and overrides["style"] != config["style"]:
            # Re-selecting a style without explicit anatomy overrides gets its
            # own defaults, rather than carrying the previous style's anatomy.
            config["parameters"] = {}
        if "parameters" in overrides and isinstance(overrides["parameters"], dict):
            overrides = {**overrides, "parameters": {**config["parameters"], **overrides["parameters"]}}
        config.update(overrides)
        return self.create(config, parent_id=job_id)

    def recover(self):
        """A stopped process restarts an interrupted job from immutable inputs."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            interrupted = db.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
            for row in interrupted:
                db.execute("UPDATE jobs SET status='queued', stage='queued', progress=0, updated_at=? WHERE id=?", (now(), row["id"]))
                db.execute("INSERT INTO events(job_id,created_at,stage,message) VALUES (?,?,?,?)",
                           (row["id"], now(), "recovered", "Worker restarted; generation will rerun from persisted inputs."))
        return len(interrupted)

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',stage='starting',updated_at=? WHERE id=?", (now(), row["id"]))
            return dict(row)

    def update(self, job_id, stage, progress, message, status="running", error=None, result=None, artifacts=None):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET status=?,stage=?,progress=?,updated_at=?,error=?,
                       result=COALESCE(?,result),artifacts=COALESCE(?,artifacts) WHERE id=?""",
                       (status, stage, progress, now(), json.dumps(error) if error else None,
                        json.dumps(result, allow_nan=False) if result is not None else None,
                        json.dumps(artifacts) if artifacts is not None else None, job_id))
            db.execute("INSERT INTO events(job_id,created_at,stage,message) VALUES (?,?,?,?)", (job_id, now(), stage, message))
        emit("job_stage", job_id=job_id, stage=stage, status=status)

    def artifact(self, job_id, name):
        job = self.get(job_id)
        artifact = next((a for a in (job["artifacts"] or []) if a["name"] == name), None)
        directory = self.directory(job_id)
        candidate = directory / name
        path = candidate.resolve()
        if not artifact or candidate.is_symlink() or path.parent != directory or not path.is_file():
            raise ProductionError("Artifact not found.", "not_found", 404)
        from artifact_integrity import ArtifactIntegrityError, verify_registered_file
        try:
            verify_registered_file(path, artifact, job.get("result"))
        except (ArtifactIntegrityError, OSError) as exc:
            raise ProductionError("Saved file failed integrity verification. Please regenerate it.",
                                  "artifact_integrity_failed", 409) from exc
        return path, artifact["mime"]


class ProductionRuntime:
    def __init__(self, data_dir, pipeline, blender_lock=None):
        self.store = JobStore(data_dir)
        self.pipeline = Path(pipeline).resolve()
        self.blender_lock = blender_lock or threading.Lock()
        self.wakeup = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.start_lock = threading.Lock()
        self.file_lock = None
        from local_bridge import LocalBridge
        self.local_bridge = LocalBridge(self.store, self.pipeline, self.wakeup)

    def blender(self):
        return shutil.which(os.environ.get("BLENDER_PATH", "blender"))

    def capabilities(self):
        asset_dir = self.pipeline / "assets" / "quaternius_standard"
        # Optional and completely isolated; older deployments are valid.
        external_available = all((asset_dir / name).is_file() for name in (
            "verified-cc0-manifest.json", "AnimationLibrary_Godot_Standard.gltf",
            "AnimationLibrary_Godot_Standard.bin"))
        binary = self.blender()
        generator = (self.pipeline / "humanoid_v4.py").is_file()
        reason = None if binary and generator else ("Blender CLI unavailable." if not binary else "Character generator unavailable.")
        return {"available": reason is None, "backend": "blender-cli", "queue_enabled": True,
                "reason": reason, "worker_running": bool(self.thread and self.thread.is_alive()),
                "max_request_bytes": MAX_REQUEST_BYTES, "max_reference_bytes": MAX_REFERENCE_BYTES,
                "max_references": MAX_REFERENCES, "parameter_ranges": PARAMETER_RANGES,
                "style_defaults": {style: {**DEFAULT_PARAMETERS, **values} for style, values in STYLE_DEFAULTS.items()},
                "body_types": BODY_TYPES,
                "rig_profiles": {"standard": {"bones": 65, "compatible": True},
                                 "detail_v1": {"bones": 87, "base_bones": 65, "extra_bones": 22,
                                               "experimental": True}}, 
                "motion_library": {"available": external_available, "id": "quaternius_cc0",
                    "license": "CC0-1.0", "version": "Standard 2025-06-10"},
                "method": "parametric_mesh_with_optional_silhouette_fit",
                "neural_reconstruction": False,
                "appearance_modes": ["preset", "reference_skin"],
                "body_part_analysis": "bounded_anthropometric_regions_v1",
                "local_preprocess": {"available": True, "contract": "3dx-local-preprocess/1",
                                     "devices": ["windows", "android"], "optional": True,
                                     "fallback": "server"}}

    def require_available(self):
        status = self.capabilities()
        if not status["available"]:
            raise ProductionError(status["reason"], "worker_unavailable", 503)

    def start(self):
        with self.start_lock:
            if self.thread and self.thread.is_alive():
                return
            # A file lock avoids resetting another live server's running job.
            import fcntl
            handle = (self.store.root / "worker.lock").open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                raise ProductionError("Another worker owns this data directory.", "worker_conflict", 503) from exc
            self.file_lock = handle
            self.store.recover()
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.run, name="3dx-production", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=5)
        # Keep ownership while a Blender process is still completing.
        if self.file_lock and (not self.thread or not self.thread.is_alive()):
            self.file_lock.close()
            self.file_lock = None

    def submit(self, value):
        self.require_available()
        if isinstance(value, dict) and value.get("motion_pack") == "quaternius_cc0" and not self.capabilities()["motion_library"]["available"]:
            raise ProductionError("Verified Quaternius CC0 library is not installed.", "motion_library_unavailable", 503)
        self.start()
        job = self.store.create(value)
        self.wakeup.set()
        return job

    def retry(self, job_id, overrides):
        self.require_available()
        if isinstance(overrides, dict) and overrides.get("motion_pack") == "quaternius_cc0" and not self.capabilities()["motion_library"]["available"]:
            raise ProductionError("Verified Quaternius CC0 library is not installed.", "motion_library_unavailable", 503)
        self.start()
        job = self.store.retry(job_id, overrides)
        self.wakeup.set()
        return job

    def run(self):
        while not self.stop_event.is_set():
            self.local_bridge.reap()
            job = self.store.claim()
            if job is None:
                self.wakeup.wait(timeout=2)
                self.wakeup.clear()
                continue
            try:
                with self.blender_lock:
                    self.process(job)
            except Exception as exc:
                code = exc.code if isinstance(exc, ProductionError) else "generation_failed"
                message = str(exc)[:1000]
                directory = self.store.directory(job["id"])
                with (directory / "generation.log").open("a", encoding="utf-8") as log:
                    log.write("\n3DX_JOB_ERROR " + code + ": " + message + "\n")
                artifacts = self.artifacts(job["id"], ["generation.log", "reconstruction.log", "reconstruction.json"])
                self.store.update(job["id"], "failed", 100, message, status="failed",
                                  error={"code": code, "message": message}, artifacts=artifacts)

    def artifacts(self, job_id, names):
        directory = self.store.directory(job_id)
        from artifact_integrity import digest_file
        # New exports pin every downloadable artifact, including BLEND/ZIP/JSON.
        # Existing SQLite entries remain valid without a migration.
        return [{"name": name, "url": f"/api/v1/jobs/{job_id}/artifacts/{name}",
                 "mime": MIMES.get(Path(name).suffix, "application/octet-stream"),
                 "bytes": (directory / name).stat().st_size,
                 "sha256": digest_file(directory / name)}
                for name in names if (directory / name).is_file()
                and not (directory / name).is_symlink()]

    def process(self, job):
        job_id = job["id"]
        directory = self.store.directory(job_id)
        config = json.loads(job["config"])
        references = config.pop("references", [])
        mocap = config.pop("mocap", None)
        reconstruction = None
        if references:
            helper_result = (config.get("preprocess_device") in ("windows", "both")
                             and (directory / "reconstruction.json").is_file())
            self.store.update(job_id, "processing_images", 10,
                              "Reusing Windows image evidence." if helper_result
                              else "Segmenting uploaded views and fitting silhouette proportions.")
            # Process image fitting separately so NumPy/Pillow memory is released
            # before launching Blender on memory-limited services.
            if not helper_result:
                inputs = [{"view": ref["view"], "path": str(directory / ref["file"]),
                           **({"pose": ref["pose"]} if "pose" in ref else {})}
                          for ref in references]
                request_file = directory / "reconstruction-request.json"
                atomic_json(request_file, {"references": inputs, "parameters": config["parameters"],
                                           "appearance_mode": config["appearance_mode"]})
                from resource_guard import run_bounded, ResourceLimitExceeded
                try:
                    with (directory / "reconstruction.log").open("wb") as log:
                        process = run_bounded(
                            [sys.executable, str(self.pipeline / "reconstruction.py"),
                             "--request", str(request_file), "--output-dir", str(directory)],
                            log=log, timeout=120, cwd=self.pipeline,
                            env={**os.environ, "OPENBLAS_NUM_THREADS": "1",
                                 "OMP_NUM_THREADS": "1", "MALLOC_ARENA_MAX": "1"})
                except subprocess.TimeoutExpired as exc:
                    raise ProductionError("Image fitting exceeded 120 seconds; check reconstruction.log.",
                                          "reconstruction_timeout", 504) from exc
                except ResourceLimitExceeded as exc:
                    raise ProductionError(
                        "A análise das imagens foi interrompida por limite de RAM. "
                        "Feche outras tarefas, use imagens menores ou tente novamente.",
                        "reconstruction_insufficient_memory", 507) from exc
                finally:
                    request_file.unlink(missing_ok=True)
                if process.returncode != 0 or not (directory / "reconstruction.json").is_file():
                    raise ProductionError("Image fitting failed; see reconstruction.log.",
                                          "reconstruction_failed", 422)
            reconstruction = json.loads((directory / "reconstruction.json").read_text(encoding="utf-8"))
            if not isinstance(reconstruction, dict) or not isinstance(reconstruction.get("parameters"), dict):
                raise ProductionError("Image processor returned an invalid result.", "reconstruction_failed", 422)
            config["parameters"].update(reconstruction["parameters"])
            # Fitted data must satisfy the same anatomy limits as direct input.
            config, _ = validate_config(config)
            # Trusted output of our image worker, never an unvalidated request option.
            palette = reconstruction.get("appearance")
            if isinstance(palette, dict) and palette.get("schema") == "3dx-appearance-palette/1":
                config["appearance"] = palette
            atomic_json(directory / "reconstruction.json", reconstruction)
            config["reconstruction"] = reconstruction
            if config["appearance_mode"] == "reference_skin":
                body_part_report = directory / "body-parts.json"
                if not body_part_report.is_file() or not isinstance(reconstruction.get("body_parts"), dict):
                    raise ProductionError("Relatório de partes anatômicas ausente.", "reconstruction_failed", 422)
                if reconstruction["body_parts"].get("schema") != "3dx-body-parts/1":
                    raise ProductionError("Schema anatômico incompatível.", "reconstruction_failed", 422)
        if mocap:
            # This is internally normalized data with a fixed directory path.
            config["mocap"] = mocap
        config["metadata"] = {"schema": "3dx-character/1", "generation_id": job_id,
                              "character_id": job["character_id"], "version": job["version"],
                              "source": "image-silhouette-estimate" if references else "parametric-test-character"}
        atomic_json(directory / "config.json", config)
        self.store.update(job_id, "generating", 30, "Blender is generating geometry, materials, rig and exports.")
        binary = self.blender()
        if not binary:
            raise ProductionError("Blender CLI unavailable.", "worker_unavailable", 503)
        command = [binary, "--background", "-t", os.environ.get("THREEDX_BLENDER_THREADS", "1"),
                   "--factory-startup", "--disable-autoexec", "--python-exit-code", "1",
                   "--python", str(self.pipeline / "humanoid_v4.py"), "--",
                   "--config", str(directory / "config.json"), "--output", str(directory / "character.glb")]
        timeout = max(10, min(3600, int(os.environ.get("THREEDX_JOB_TIMEOUT", "900"))))
        from resource_guard import run_bounded, ResourceLimitExceeded
        try:
            with (directory / "generation.log").open("wb") as log:
                completed = run_bounded(
                    command, log=log, timeout=timeout, cwd=self.pipeline,
                    env={**os.environ, "MALLOC_ARENA_MAX": "1", "OPENBLAS_NUM_THREADS": "1",
                         "OMP_NUM_THREADS": "1", "THREEDX_BLENDER_THREADS": "1",
                          "THREEDX_FULL_ROUNDTRIP": "0"})
        except subprocess.TimeoutExpired as exc:
            raise ProductionError(f"Blender exceeded the {timeout} second execution limit.", "blender_timeout", 504) from exc
        except ResourceLimitExceeded as exc:
            raise ProductionError(
                "Blender reached the memory safety limit. The server was kept online; "
                "try Preview quality or use a worker with at least 2 GiB RAM.",
                "insufficient_memory", 507) from exc
        if completed.returncode in (-9, 137):
            raise ProductionError(
                "Blender was killed by the host, likely due to insufficient memory. "
                "Try Preview quality or a worker with at least 2 GiB RAM.",
                "blender_killed", 507)
        if completed.returncode != 0:
            raise ProductionError(f"Blender failed (exit {completed.returncode}). Download generation.log for details.", "blender_failed", 422)
        self.store.update(job_id, "validating", 85, "Checking generated GLB buffers, geometry and exported artifacts.")
        validator = load_module(self.pipeline / "validate_glb.py", "threedx_glb_validator")
        geometry = validator.inspect_glb(directory / "character.glb")
        geometry.pop("file", None)
        report_path = directory / "character-report.json"
        if not report_path.is_file() or not (directory / "character.blend").is_file():
            raise ProductionError("Blender did not produce its editable project and QA report.", "missing_artifact", 422)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("status") not in ("passed", "succeeded", "completed"):
            raise ProductionError("Blender QA report did not confirm success.", "qa_failed", 422)
        if config["rig"] and config.get("rig_profile") == "detail_v1" and geometry.get("joints") != 87:
            raise ProductionError("Advanced rig export has missing joints.", "qa_failed", 422)
        if config["rig"] and geometry.get("skins", 0) < 1:
            raise ProductionError("Requested rig is missing from exported GLB.", "qa_failed", 422)
        if config["animations"] and geometry.get("animations", 0) < 1:
            raise ProductionError("Requested animations are missing from exported GLB.", "qa_failed", 422)
        method = "silhouette_parametric_fit" if references else "parametric_generation"
        integration = {
            "schema": "3dx-asset-package/1", "generation_id": job_id,
            "character_id": job["character_id"], "version": job["version"],
            "name": config["name"], "units": "meters", "up_axis": "Y",
            "base_license": report.get("base", {}).get("license", "unknown"),
            "glb": "character.glb", "glb_sha256": geometry["sha256"],
            "blend": "character.blend", "report": "character-report.json",
            "animations": geometry.get("animation_names", []), "joints": geometry.get("joints", 0),
            "motion_pack": config.get("motion_pack", "procedural"),
            "rig_profile": config.get("rig_profile", "standard"),
            "external_motion": report.get("external_motion"),
            "estimated": bool(references), "reconstruction_method": method,
            "appearance_method": config.get("appearance", {}).get("method", "preset_only"),
            "appearance_mode": config["appearance_mode"],
            "body_parts_report": "body-parts.json" if config["appearance_mode"] == "reference_skin" else None,
            "source_identity": "test_character", "production_game_modified": False,
            "semantic_contract": "3dx-glb-semantics/1",
            "runtime_quality_contract": "3dx-runtime-quality/1",
            "contracts": {
                "anatomy": {"schema": "3dx-anatomy/1",
                            "body_type": config["body_type"], "parameters": config["parameters"]},
                "wardrobe": {"schema": "3dx-wardrobe/1",
                             "outfit": config["outfit"], "slots": config["garment_slots"],
                             "garments": report.get("garment_parts")},
                "rig": {"schema": "3dx-rig/1", "enabled": config["rig"],
                        "profile": config["rig_profile"] if config["rig"] else "none",
                        "joints": geometry.get("joints", 0)},
                "animation": {"schema": "3dx-animation/1", "enabled": config["animations"],
                              "clips": geometry.get("animation_names", [])},
            },
        }
        atomic_json(directory / "integration.json", integration)
        guardian = load_module(self.pipeline / "package_guardian.py", "threedx_package_guardian")
        try:
            guardian_report = guardian.validate_package(integration, geometry, directory)
        except guardian.PackageContractError as exc:
            raise ProductionError("3DX Guardian rejected package: " + str(exc),
                                  "integration_incompatible", 422) from exc
        atomic_json(directory / "package-guardian.json", guardian_report)
        names = ["character.glb", "character.blend", "character-report.json", "integration.json",
                 "package-guardian.json", "generation.log"]
        if reconstruction is not None:
            names.extend(["reconstruction.json", "reconstruction.log"])
            if config["appearance_mode"] == "reference_skin":
                names.append("body-parts.json")
                names.extend(p.name for p in sorted(directory.glob("part-mask-*.png"))
                             if re.fullmatch(r"part-mask-\d{2}-(?:front|back|left|right)-[a-z_]+\.png", p.name))
            names.extend(path.name for path in sorted(directory.glob("silhouette-*.png"))
                         if re.fullmatch(r"silhouette-\d{2}-(front|back|left|right|additional)\.png", path.name))
        with zipfile.ZipFile(directory / "character-bundle.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                archive.write(directory / name, name)
        names.append("character-bundle.zip")
        result = {"geometry": geometry, "generator": report,
                  "method": method, "integration": integration,
                  "estimated": bool(references), "reconstruction": reconstruction}
        self.store.update(job_id, "completed", 100, "Validated files are registered in the character catalog.",
                          status="succeeded", result=result, artifacts=self.artifacts(job_id, names))
