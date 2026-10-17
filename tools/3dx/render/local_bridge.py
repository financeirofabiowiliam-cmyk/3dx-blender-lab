"""Opt-in secure Windows companion queue for reference-only preprocessing.

Existing Blender GLB generation stays on the Render worker. The companion pulls
short-lived, authenticated leases and returns bounded reconstruction evidence.
Missing/offline companions fall back to the existing Render pipeline.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets

from production import ProductionError, PARAMETER_RANGES, now

PIPELINE_FILES = (
    "reconstruction.py", "body_part_analysis.py", "component_modules.py",
    "component_face.py", "component_hair.py", "component_clothing.py",
    "component_accessories.py", "hair_volume_fit.py", "view_fusion.py",
    "pose_landmark_fit.py",
)
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
LEASE_SECONDS = 600
WAIT_SECONDS = 100
MASK_NAME = re.compile(r"(?:silhouette-\d{2}-(?:front|back|left|right|additional)|part-mask-\d{2}-(?:front|back|left|right)-[a-z_]+)\.png")


class LocalBridge:
    def __init__(self, store, pipeline, wakeup=None):
        self.store = store
        self.pipeline = Path(pipeline)
        self.wakeup = wakeup
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS local_leases(
                job_id TEXT PRIMARY KEY, lease_hash TEXT NOT NULL,
                deadline TEXT NOT NULL)""")

    def manifest(self):
        return {name: hashlib.sha256((self.pipeline / name).read_bytes()).hexdigest()
                for name in PIPELINE_FILES}

    def script(self, name):
        if name not in PIPELINE_FILES:
            raise ProductionError("Unknown helper module.", "not_found", 404)
        return (self.pipeline / name).read_bytes()

    def reap(self):
        """Recover stalled stages without discarding work completed by other helpers."""
        timestamp = now()
        waiting_before = (datetime.now(timezone.utc) -
                          timedelta(seconds=WAIT_SECONDS)).isoformat(timespec="milliseconds")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            waiting = db.execute(
                "SELECT id,status,stage,config FROM jobs WHERE status='awaiting_local' AND updated_at<?",
                (waiting_before,)
            ).fetchall()
            leased = db.execute("""SELECT j.id,j.status,j.stage,j.config FROM jobs j
                                   JOIN local_leases l ON l.job_id=j.id
                                   WHERE j.status='local_processing' AND l.deadline<?""",
                                (timestamp,)).fetchall()
            orphans = db.execute("""SELECT j.id,j.status,j.stage,j.config FROM jobs j
                                    LEFT JOIN local_leases l ON l.job_id=j.id
                                    WHERE j.status='local_processing' AND l.job_id IS NULL""").fetchall()
            rows = {j["id"]: j for j in waiting + leased + orphans}
            for job in rows.values():
                cfg = json.loads(job["config"])
                # Hybrid mode runs Android -> Windows -> server. If Android
                # times out, Windows still receives its fair chance.
                to_windows = (cfg.get("preprocess_device") == "both" and
                              job["stage"] in ("awaiting_android", "processing_android"))
                status = "awaiting_local" if to_windows else "queued"
                stage = "awaiting_windows" if to_windows else "queued"
                db.execute("UPDATE jobs SET status=?,stage=?,progress=0,updated_at=? WHERE id=?",
                           (status,stage,timestamp,job["id"]))
                db.execute("DELETE FROM local_leases WHERE job_id=?", (job["id"],))
                db.execute("INSERT INTO events(job_id,created_at,stage,message) VALUES (?,?,?,?)",
                           (job["id"],timestamp,stage,
                            "Android unavailable; trying Windows next."
                            if to_windows else "Device unavailable; normal Render pipeline restored."))
        if rows and self.wakeup:
            self.wakeup.set()
        return len(rows)

    def claim(self, device='windows'):
        if device not in ('windows', 'android'):
            raise ProductionError('Unsupported worker device.', 'invalid_worker', 422)
        self.reap()
        timestamp = now()
        lease = secrets.token_urlsafe(32)
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat(timespec="milliseconds")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            candidates = db.execute("""SELECT * FROM jobs WHERE status='awaiting_local'
                                        ORDER BY created_at LIMIT 12""").fetchall()
            row = next((j for j in candidates if (
                json.loads(j["config"]).get("preprocess_device") == device or (
                    json.loads(j["config"]).get("preprocess_device") == "both" and
                    j["stage"] == ("awaiting_android" if device == "android" else "awaiting_windows")
                ))), None)
            if row is None:
                return None
            jid = row["id"]
            job_stage = ("processing_" + device
                         if json.loads(row["config"]).get("preprocess_device") == "both"
                         else "processing_local")
            db.execute("""UPDATE jobs SET status='local_processing',stage=?,
                           progress=15,updated_at=? WHERE id=? AND status='awaiting_local'""",
                       (job_stage,timestamp,jid))
            db.execute("INSERT OR REPLACE INTO local_leases(job_id,lease_hash,deadline) VALUES (?,?,?)",
                       (jid,hashlib.sha256(lease.encode()).hexdigest(),deadline))
        cfg = json.loads(row["config"])
        refs = []
        directory = self.store.directory(jid)
        for ref in cfg["references"]:
            data = (directory / ref["file"]).read_bytes()
            refs.append({"view":ref["view"],"name":ref["name"],
                         "data":base64.b64encode(data).decode("ascii"),
                         **({"pose":ref["pose"]} if "pose" in ref else {})})
        return {"job_id":jid,"lease":lease,"expires_at":deadline,
                "contract":"3dx-local-preprocess/1",
                "parameters":cfg["parameters"],"appearance_mode":cfg["appearance_mode"],
                "references":refs,"modules":self.manifest() if device == "windows" else {},
                "device":device}

    def complete_mobile(self, request):
        """Verify resized images before adding them to the original 3DX job.

        Android prepares pixels only. The server still runs trusted Python
        silhouette reconstruction and Blender generation with existing QA.
        """
        import io
        from PIL import Image, ImageOps, UnidentifiedImageError
        if not isinstance(request, dict) or set(request) != {"job_id", "lease", "references"}:
            raise ProductionError("Invalid Android helper result.", "invalid_mobile_result", 422)
        jid, secret, refs = (request[key] for key in ("job_id", "lease", "references"))
        directory = self.store.directory(jid)
        if not isinstance(secret, str) or not 20 <= len(secret) <= 128:
            raise ProductionError("Invalid Android lease.", "invalid_mobile_result", 422)
        if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
            raise ProductionError("Invalid reference count.", "invalid_mobile_result", 422)
        prepared = []
        total = 0
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"view", "data"}:
                raise ProductionError("Invalid mobile reference.", "invalid_mobile_result", 422)
            encoded = ref["data"]
            if not isinstance(encoded, str) or len(encoded) > 5592416:
                raise ProductionError("Image exceeds 4 MiB.", "image_too_large", 413)
            try:
                raw = base64.b64decode(encoded, validate=True)
                if not raw or len(raw) > 4 * 1024 * 1024:
                    raise ValueError("Image too large")
                with Image.open(io.BytesIO(raw)) as pic:
                    if pic.format not in ("PNG", "JPEG") or getattr(pic, "n_frames", 1) != 1:
                        raise ValueError("Unsupported image")
                    if min(pic.size) < 32 or max(pic.size) > 768:
                        raise ValueError("Invalid image size")
                    pic.verify()
                with Image.open(io.BytesIO(raw)) as pic:
                    image = ImageOps.exif_transpose(pic).convert("RGBA")
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    normalized = buffer.getvalue()
            except (OSError, ValueError, binascii.Error, UnidentifiedImageError) as exc:
                raise ProductionError("Invalid Android image.", "invalid_mobile_result", 422) from exc
            if len(normalized) > 4 * 1024 * 1024:
                raise ProductionError("Normalized image exceeds 4 MiB.", "image_too_large", 413)
            total += len(normalized)
            if total > 12 * 1024 * 1024:
                raise ProductionError("Too many mobile image bytes.", "image_too_large", 413)
            prepared.append((ref["view"], normalized, image.width, image.height))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,stage,config FROM jobs WHERE id=?", (jid,)).fetchone()
            token = db.execute("SELECT lease_hash,deadline FROM local_leases WHERE job_id=?", (jid,)).fetchone()
            if (row is None or row["status"] != "local_processing" or token is None
                    or not hmac.compare_digest(token["lease_hash"], hashlib.sha256(secret.encode()).hexdigest())
                    or token["deadline"] < now()):
                raise ProductionError("Android lease expired.", "expired_helper_lease", 409)
            cfg = json.loads(row["config"])
            original = cfg["references"]
            if (cfg.get("preprocess_device") not in ("android", "both")
                    or (cfg.get("preprocess_device") == "both" and row["stage"] != "processing_android")
                    or len(original) != len(prepared)
                    or any(original[i]["view"] != item[0] for i,item in enumerate(prepared))):
                raise ProductionError("Mobile job mismatch.", "invalid_mobile_result", 422)
            for i,(_,data,width,height) in enumerate(prepared):
                target = directory / original[i]["file"]
                part = target.with_suffix(".android-part")
                part.write_bytes(data)
                part.replace(target)
                original[i].update({"width":width,"height":height,"bytes":len(data)})
            timestamp = now()
            next_windows = cfg.get("preprocess_device") == "both"
            next_status = "awaiting_local" if next_windows else "queued"
            next_stage = "awaiting_windows" if next_windows else "queued"
            db.execute("UPDATE jobs SET config=?,status=?,stage=?,progress=0,updated_at=? WHERE id=?",
                       (json.dumps(cfg,allow_nan=False),next_status,next_stage,timestamp,jid))
            db.execute("DELETE FROM local_leases WHERE job_id=?", (jid,))
            db.execute("INSERT INTO events(job_id,created_at,stage,message) VALUES (?,?,?,?)",
                       (jid,timestamp,next_stage,
                        "Android prepared references; Windows analysis is next."
                        if next_windows else
                        "Android prepared references; trusted 3DX pipeline queued."))
        if self.wakeup:
            self.wakeup.set()
        return self.store.get(jid)

    def abandon(self, request):
        """Immediately return a failed/low-memory local task to Render."""
        if not isinstance(request, dict) or set(request) != {"job_id", "lease"}:
            raise ProductionError("Invalid lease request.", "invalid_helper_result", 422)
        jid = request["job_id"]
        self.store.directory(jid)
        secret = request["lease"]
        if not isinstance(secret, str) or len(secret) > 128:
            raise ProductionError("Invalid lease.", "invalid_helper_result", 422)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,stage,config FROM jobs WHERE id=?", (jid,)).fetchone()
            lease = db.execute("SELECT lease_hash FROM local_leases WHERE job_id=?", (jid,)).fetchone()
            if (row is None or row["status"] != "local_processing" or lease is None
                    or not hmac.compare_digest(lease["lease_hash"],
                                               hashlib.sha256(secret.encode()).hexdigest())):
                raise ProductionError("Expired helper lease.", "expired_helper_lease", 409)
            mode = json.loads(row["config"]).get("preprocess_device")
            to_windows = mode == "both" and row["stage"] == "processing_android"
            next_status = "awaiting_local" if to_windows else "queued"
            next_stage = "awaiting_windows" if to_windows else "queued"
            timestamp = now()
            db.execute("UPDATE jobs SET status=?,stage=?,progress=0,updated_at=? WHERE id=?",
                       (next_status,next_stage,timestamp,jid))
            db.execute("DELETE FROM local_leases WHERE job_id=?", (jid,))
            db.execute("INSERT INTO events(job_id,created_at,stage,message) VALUES (?,?,?,?)",
                       (jid, timestamp, next_stage,
                        "Android worker stopped; Windows may continue."
                        if to_windows else "Device worker stopped; Render fallback started."))
        if self.wakeup:
            self.wakeup.set()
        return self.store.get(jid)

    def complete(self, request):
        if not isinstance(request,dict) or set(request)!={"job_id","lease","report","files"}:
            raise ProductionError("Invalid helper result.", "invalid_helper_result", 422)
        jid,lease,report,files=(request[k] for k in ("job_id","lease","report","files"))
        directory=self.store.directory(jid)
        if not isinstance(lease,str) or len(lease)>128 or not isinstance(report,dict) or not isinstance(files,dict):
            raise ProductionError("Invalid helper payload.", "invalid_helper_result", 422)
        if (report.get("schema")!="3dx-reconstruction/1" or
            report.get("method")!="silhouette_measurement_fit" or
            not isinstance(report.get("parameters"),dict) or
            not isinstance(report.get("views"),list)):
            raise ProductionError("Invalid reconstruction contract.", "invalid_helper_result", 422)
        for key,value in report["parameters"].items():
            if key not in PARAMETER_RANGES or type(value) not in (float,int):
                raise ProductionError("Invalid anatomical value.", "invalid_helper_result", 422)
            low,high=PARAMETER_RANGES[key]
            if not low<=value<=high:
                raise ProductionError("Anatomy outside limits.", "invalid_helper_result", 422)
        body=report.get("body_parts")
        if body is not None and (not isinstance(body,dict) or body.get("schema")!="3dx-body-parts/1"):
            raise ProductionError("Invalid body part contract.", "invalid_helper_result", 422)
        if len(files)>200 or any(not MASK_NAME.fullmatch(name) for name in files):
            raise ProductionError("Unknown helper artifact.", "invalid_helper_result", 422)
        staged={}
        total=0
        for name,encoded in files.items():
            if not isinstance(encoded,str) or len(encoded)>((MAX_ARTIFACT_BYTES+2)//3)*4+8:
                raise ProductionError("Oversized helper artifact.", "invalid_helper_result", 413)
            try:
                data=base64.b64decode(encoded,validate=True)
            except (ValueError,binascii.Error) as exc:
                raise ProductionError("Invalid helper encoding.", "invalid_helper_result", 422) from exc
            if len(data)>MAX_ARTIFACT_BYTES or data[:8]!=b"\x89PNG\r\n\x1a\n":
                raise ProductionError("Invalid PNG artifact.", "invalid_helper_result", 422)
            staged[name]=data
            total+=len(data)
            if total>MAX_TOTAL_BYTES:
                raise ProductionError("Too many helper artifacts.", "invalid_helper_result", 413)
        serialized=json.dumps(report,allow_nan=False,ensure_ascii=False).encode("utf-8")
        if len(serialized)>2*1024*1024:
            raise ProductionError("Oversized reconstruction report.", "invalid_helper_result", 413)
        # Check lease again while holding a write transaction. Stale results
        # cannot replace already-fallback or already-generated character jobs.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT status,stage,config FROM jobs WHERE id=?",(jid,)).fetchone()
            token=db.execute("SELECT lease_hash,deadline FROM local_leases WHERE job_id=?",(jid,)).fetchone()
            if (row is None or row["status"]!="local_processing" or token is None
                    or not hmac.compare_digest(token["lease_hash"],hashlib.sha256(lease.encode()).hexdigest())
                    or token["deadline"]<now()):
                raise ProductionError("Helper lease expired; server fallback already active.",
                                      "expired_helper_lease",409)
            cfg=json.loads(row["config"])
            if cfg.get("preprocess_device") == "both" and row["stage"] != "processing_windows":
                raise ProductionError("Android stage not completed or skipped.", "invalid_worker", 409)
            views={r["view"] for r in cfg.get("references",[])}
            if len(report["views"])!=len(cfg.get("references",[])) or {v.get("view") for v in report["views"] if isinstance(v,dict)}!=views:
                raise ProductionError("Reference views disagree.", "invalid_helper_result",422)
            if cfg["appearance_mode"]=="reference_skin" and body is None:
                raise ProductionError("Missing body-part evidence.", "invalid_helper_result",422)
            written=[]
            try:
                for name,data in staged.items():
                    tmp=directory/(name+".part")
                    tmp.write_bytes(data); written.append(tmp)
                    tmp.replace(directory/name)
                path=directory/"reconstruction.json"
                tmp=directory/"reconstruction.json.part"
                tmp.write_bytes(serialized); written.append(tmp);tmp.replace(path)
                if body is not None:
                    tmp=directory/"body-parts.json.part"
                    tmp.write_text(json.dumps(body,ensure_ascii=False,allow_nan=False),encoding="utf-8")
                    written.append(tmp); tmp.replace(directory/"body-parts.json")
                (directory/"reconstruction.log").write_text(
                    "3DX Windows helper: bounded CPU silhouette analysis completed.\n",encoding="utf-8")
                db.execute("UPDATE jobs SET status='queued',stage='queued',progress=0,updated_at=? WHERE id=?",
                           (now(),jid))
                db.execute("DELETE FROM local_leases WHERE job_id=?",(jid,))
                db.execute("INSERT INTO events(job_id,created_at,stage,message) VALUES (?,?,?,?)",
                           (jid,now(),"queued","Validated Windows silhouette analysis; Blender queued on Render."))
            finally:
                for path in written:
                    path.unlink(missing_ok=True)
        if self.wakeup:self.wakeup.set()
        return self.store.get(jid)
