"""3DX Windows Hybrid Lite: durable opt-in and fallbacks, without Blender."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from io import BytesIO

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from production import JobStore, ProductionError
from local_bridge import LocalBridge, PIPELINE_FILES


class LocalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.store=JobStore(root/"jobs")
        self.pipeline=root/"pipeline"
        self.pipeline.mkdir()
        for name in PIPELINE_FILES:
            (self.pipeline/name).write_text("# 3DX fixture for "+name,encoding="utf-8")
        self.event=threading.Event()
        self.bridge=LocalBridge(self.store,self.pipeline,self.event)
        image=Image.new("RGBA",(64,128),(0,0,0,0))
        image.paste((20,30,40,255),(16,10,48,120))
        output=BytesIO()
        image.save(output,"PNG")
        self.reference={"view":"front","name":"front.png",
                        "data":base64.b64encode(output.getvalue()).decode("ascii")}
        self.parameters={"height":1.8,"head_scale":1.0}
    def job(self,mode="windows",references=True):
        return self.store.create({"name":"Hybrid Lite fixture","preprocess_device":mode,
                                  "references":[self.reference] if references else [],
                                  "animations":True,"rig":True})
    def test_opt_in_isolated_from_default_and_no_image_jobs(self):
        self.assertEqual(self.job("server")["status"],"queued")
        self.assertEqual(self.job("windows",False)["status"],"queued")
        self.assertIsNone(self.bridge.claim())
    def test_lease_upload_reuses_normal_blender_queue(self):
        j=self.job()
        self.assertEqual(j["status"],"awaiting_local")
        task=self.bridge.claim()
        self.assertEqual(task["job_id"],j["id"])
        self.assertEqual(task["contract"],"3dx-local-preprocess/1")
        self.assertEqual(task["references"][0]["view"],"front")
        self.assertEqual(task["modules"]["reconstruction.py"],
                         hashlib.sha256((self.pipeline/"reconstruction.py").read_bytes()).hexdigest())
        self.assertEqual(self.store.get(j["id"])["status"],"local_processing")
        report={"schema":"3dx-reconstruction/1","method":"silhouette_measurement_fit",
                "parameters":{"height":1.8,"head_scale":1.0},"views":[{"view":"front"}]}
        done=self.bridge.complete({"job_id":j["id"],"lease":task["lease"],
                                   "report":report,"files":{}})
        self.assertEqual(done["status"],"queued")
        self.assertTrue((self.store.directory(j["id"])/"reconstruction.json").is_file())
        self.assertTrue(self.event.is_set())
        self.assertEqual(self.store.claim()["id"],j["id"])
        with self.assertRaises(ProductionError):
            self.bridge.complete({"job_id":j["id"],"lease":task["lease"],
                                  "report":report,"files":{}})
    def test_android_worker_scoping_and_verified_image_upload(self):
        from PIL import Image
        import io
        pc = self.job("windows")
        android = self.job("android")
        self.assertEqual(self.bridge.claim("windows")["job_id"], pc["id"])
        task = self.bridge.claim("android")
        self.assertEqual(task["job_id"], android["id"])
        self.assertEqual(task["device"], "android")
        self.assertEqual(task["modules"], {})
        picture = Image.new("RGBA", (48, 72), (20, 40, 60, 255))
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        ref = {"view": "front", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}
        finished = self.bridge.complete_mobile(
            {"job_id": android["id"], "lease": task["lease"], "references": [ref]})
        self.assertEqual(finished["status"], "queued")
        updated = self.store.get_internal(android["id"])
        saved = json.loads(updated["config"])["references"][0]
        self.assertEqual([saved["width"], saved["height"]], [48, 72])
        self.assertTrue((self.store.directory(android["id"])/saved["file"]).is_file())
        self.assertEqual(self.store.claim()["id"], android["id"])
        with self.assertRaises(ProductionError) as expired:
            self.bridge.complete_mobile(
                {"job_id": android["id"], "lease": task["lease"], "references": [ref]})
        self.assertEqual(expired.exception.status, 409)

    def test_android_helper_rejects_device_mismatch_and_wrong_reference(self):
        win = self.job("windows")
        with self.assertRaises(ProductionError):
            self.bridge.claim("other")
        task = self.bridge.claim("windows")
        picture = Image.new("RGBA", (48, 72), (20, 40, 60, 255))
        buffer = BytesIO(); picture.save(buffer, format="PNG")
        good = base64.b64encode(buffer.getvalue()).decode("ascii")
        with self.assertRaises(ProductionError):
            self.bridge.complete_mobile({
                "job_id":win["id"], "lease":task["lease"],
                "references":[{"view":"front","data":good}]})
        self.assertEqual(self.store.get(win["id"])["status"], "local_processing")
        self.assertEqual(self.bridge.abandon({"job_id":win["id"], "lease":task["lease"]})["status"], "queued")
        android = self.job("android")
        task = self.bridge.claim("android")
        with self.assertRaises(ProductionError):
            self.bridge.complete_mobile({"job_id":android["id"],"lease":task["lease"],
                                         "references":[{"view":"back","data":good}]})
        self.assertEqual(self.store.get(android["id"])["status"], "local_processing")

    def test_both_devices_one_job_android_then_windows(self):
        j = self.job("both")
        self.assertEqual(j["status"], "awaiting_local")
        self.assertEqual(self.store.get_internal(j["id"])["stage"], "awaiting_android")
        self.assertIsNone(self.bridge.claim("windows"))
        mobile = self.bridge.claim("android")
        self.assertEqual(mobile["job_id"], j["id"])
        self.assertEqual(self.store.get_internal(j["id"])["stage"], "processing_android")
        with self.assertRaises(ProductionError):
            self.bridge.claim("invalid")
        # A failed Android preparation should still give Windows a chance.
        self.assertEqual(self.bridge.abandon({
            "job_id": j["id"], "lease": mobile["lease"]})["status"], "awaiting_local")
        self.assertEqual(self.store.get_internal(j["id"])["stage"], "awaiting_windows")
        self.assertIsNone(self.bridge.claim("android"))
        windows = self.bridge.claim("windows")
        self.assertEqual(windows["job_id"], j["id"])
        self.assertEqual(self.store.get_internal(j["id"])["stage"], "processing_windows")
        report = {"schema": "3dx-reconstruction/1", "method": "silhouette_measurement_fit",
                  "parameters": {"height": 1.8, "head_scale": 1.0},
                  "views": [{"view":"front"}]}
        done = self.bridge.complete({"job_id":j["id"], "lease":windows["lease"],
                                      "report":report,"files":{}})
        self.assertEqual(done["status"], "queued")
        self.assertTrue((self.store.directory(j["id"]) / "reconstruction.json").is_file())

    def test_both_android_success_moves_to_windows(self):
        j = self.job("both")
        mobile = self.bridge.claim("android")
        png = Image.new("RGBA", (64, 96), (20, 60, 80, 255))
        buffer = BytesIO(); png.save(buffer, "PNG")
        ref = {"view":"front", "data":base64.b64encode(buffer.getvalue()).decode("ascii")}
        state = self.bridge.complete_mobile({
            "job_id": j["id"], "lease": mobile["lease"], "references":[ref]})
        self.assertEqual(state["status"], "awaiting_local")
        self.assertEqual(self.store.get_internal(j["id"])["stage"], "awaiting_windows")
        self.assertEqual(self.bridge.claim("windows")["job_id"], j["id"])

    def test_both_fallback_after_missing_android_and_windows(self):
        j = self.job("both")
        stale = (datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat(timespec="milliseconds")
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stale,j["id"]))
        self.assertEqual(self.bridge.reap(),1)
        self.assertEqual(self.store.get_internal(j["id"])["stage"], "awaiting_windows")
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stale,j["id"]))
        self.assertEqual(self.bridge.reap(),1)
        self.assertEqual(self.store.get(j["id"])["status"], "queued")

    def test_wrong_lease_cannot_publish_evidence(self):
        j=self.job()
        task=self.bridge.claim()
        with self.assertRaises(ProductionError) as error:
            self.bridge.abandon({"job_id":j["id"],"lease":"wrong"})
        self.assertEqual(error.exception.status,409)
        self.assertEqual(self.store.get(j["id"])["status"],"local_processing")
        self.bridge.abandon({"job_id":j["id"],"lease":task["lease"]})
        self.assertEqual(self.store.get(j["id"])["status"],"queued")
    def test_expired_task_returns_to_original_server_queue(self):
        j=self.job()
        task=self.bridge.claim()
        with self.store.connect() as db:
            db.execute("UPDATE local_leases SET deadline=? WHERE job_id=?",
                       ((datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat(timespec="milliseconds"),j["id"]))
        self.assertEqual(self.bridge.reap(),1)
        self.assertEqual(self.store.get(j["id"])["status"],"queued")
        with self.assertRaises(ProductionError) as error:
            self.bridge.abandon({"job_id":j["id"],"lease":task["lease"]})
        self.assertEqual(error.exception.status,409)
    def test_unclaimed_task_times_out(self):
        j=self.job()
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET updated_at=? WHERE id=?",
                       ((datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat(timespec="milliseconds"),j["id"]))
        self.assertEqual(self.bridge.reap(),1)
        self.assertEqual(self.store.get(j["id"])["status"],"queued")
    def test_script_allowlist_is_not_an_arbitrary_file_browser(self):
        self.assertIn(b"fixture",self.bridge.script("reconstruction.py"))
        with self.assertRaises(ProductionError):
            self.bridge.script("../../server.py")
    def test_invalid_remote_anatomy_is_refused(self):
        j=self.job()
        t=self.bridge.claim()
        with self.assertRaises(ProductionError):
            self.bridge.complete({"job_id":j["id"],"lease":t["lease"],
                                  "report":{"schema":"3dx-reconstruction/1",
                                            "method":"silhouette_measurement_fit",
                                            "parameters":{"height":999},"views":[{"view":"front"}]},
                                  "files":{}})
        self.assertEqual(self.store.get(j["id"])["status"],"local_processing")


if __name__=="__main__":
    unittest.main()
