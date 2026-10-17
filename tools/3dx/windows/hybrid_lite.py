"""3DX Hybrid Lite: opt-in Windows 10 (64-bit) image analysis companion.

Run with Python 3.11+ and NumPy/Pillow. No Blender, admin rights, local web
server, inbound ports or paid services required. Keep the Render access token
private; it is never written to disk.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ORIGIN = "https://threedx-studio-blender-qa.onrender.com"
CONTRACT = "3dx-local-preprocess/1"
MIB = 1024 * 1024
IDLE_SECONDS = 12
MODULE = re.compile(r"^[a-z_]+\.py$")
MASK = re.compile(r"(?:silhouette-\d{2}-(?:front|back|left|right|additional)|part-mask-\d{2}-(?:front|back|left|right)-[a-z_]+)\.png")


def available_ram():
    """Windows physical available RAM in bytes, or None."""
    if os.name != "nt":
        return None
    class Status(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                    ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                    ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                    ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                    ("extra", ctypes.c_ulonglong)]
    mem = Status()
    mem.length = ctypes.sizeof(mem)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        return int(mem.available)
    return None


def request(token, path, payload=None):
    if not path.startswith("/api/v1/worker/") or ".." in path:
        raise ValueError("Endpoint not allowed.")
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if payload is not None else None
    req = Request(ORIGIN + path, data=body,
                  headers={"Authorization": "Bearer " + token,
                           "X-3DX-Asset-Contract": "3dx-asset-package/1",
                           **({"Content-Type": "application/json"} if body is not None else {})},
                  method="POST" if body is not None else "GET")
    with urlopen(req, timeout=80) as reply:
        result = reply.read(35 * MIB)
        if len(result) >= 35 * MIB:
            raise ValueError("Server response too large.")
        if path.startswith("/api/v1/worker/pipeline/"):
            return result
        return json.loads(result)


def modules(token, hashes, target):
    if not isinstance(hashes, dict) or "reconstruction.py" not in hashes or len(hashes) > 20:
        raise ValueError("Unsupported 3DX module manifest.")
    target.mkdir(parents=True, exist_ok=True)
    for name, expected in hashes.items():
        if not isinstance(name, str) or not MODULE.fullmatch(name) or not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Invalid 3DX module manifest.")
        path = target / name
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
            continue
        data = request(token, "/api/v1/worker/pipeline/" + name)
        if len(data) > 256 * 1024 or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("3DX module integrity verification failed.")
        temp = path.with_suffix(".partial")
        temp.write_bytes(data)
        temp.replace(path)


def stop_child(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=6, check=False)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def analyze(token, task, cache, should_stop=None, start_free_mb=950, stop_free_mb=650, on_progress=None):
    if task.get("contract") != CONTRACT or not re.fullmatch(r"[a-f0-9]{32}", task.get("job_id", "")):
        raise ValueError("Unexpected 3DX helper task contract.")
    memory = available_ram()
    if memory is not None and memory < start_free_mb * MIB:
        raise MemoryError("Pouca RAM livre para análise local. O Render assumirá a tarefa.")
    if callable(on_progress):
        on_progress("Sincronizando módulos oficiais de reconstrução…")
    modules(token, task["modules"], cache)
    if callable(should_stop) and should_stop():
        raise InterruptedError("Processamento interrompido pelo usuário.")
    with tempfile.TemporaryDirectory(prefix="3dx-hybrid-") as temp:
        directory = Path(temp)
        references = []
        for index, ref in enumerate(task["references"]):
            if index >= 8 or ref["view"] not in ("front", "back", "left", "right", "additional"):
                raise ValueError("Unsupported reference view.")
            image = base64.b64decode(ref["data"], validate=True)
            if len(image) > 4 * MIB or not (image.startswith(b"\x89PNG\r\n\x1a\n") or image.startswith(b"\xff\xd8\xff")):
                raise ValueError("Invalid image.")
            path = directory / ("reference-%02d.png" % index)
            path.write_bytes(image)
            references.append({"view":ref["view"], "path":str(path),
                               **({"pose":ref["pose"]} if "pose" in ref else {})})
        request_path=directory / "request.json"
        request_path.write_text(json.dumps({"references":references,
                                            "parameters":task["parameters"],
                                            "appearance_mode":task["appearance_mode"]},
                                           ensure_ascii=False),encoding="utf-8")
        command=[sys.executable, str(cache/"reconstruction.py"),
                 "--request",str(request_path),"--output-dir",str(directory)]
        env={**os.environ,"OPENBLAS_NUM_THREADS":"1","OMP_NUM_THREADS":"1",
             "MALLOC_ARENA_MAX":"1","PYTHONNOUSERSITE":"0"}
        if callable(on_progress):
            on_progress("Analisando imagens — uma tarefa por vez…")
        with (directory/"worker.log").open("wb") as log:
            # A PyInstaller GUI executable cannot serve as a Python script interpreter.
            if getattr(sys, "frozen", False):
                command=["py", "-3"]+command[1:]
            proc=subprocess.Popen(command, stdin=subprocess.DEVNULL,stdout=log,
                                  stderr=subprocess.STDOUT,env=env,cwd=str(cache),
                                  creationflags=getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0))
            started=time.monotonic()
            try:
                while proc.poll() is None:
                    if time.monotonic()-started>300:
                        raise TimeoutError("Local analysis exceeded five minutes.")
                    free=available_ram()
                    if callable(should_stop) and should_stop():
                        raise InterruptedError("Interrompido pelo usuário.")
                    if free is not None and free < stop_free_mb * MIB:
                        raise MemoryError("Pouca memória disponível: tarefa transferida ao Render.")
                    time.sleep(0.5)
            finally:
                stop_child(proc)
        if proc.returncode:
            raise RuntimeError("Local analysis failed; Render will retry automatically.")
        report_path=directory/"reconstruction.json"
        report=json.loads(report_path.read_text(encoding="utf-8"))
        files={}
        total=0
        for path in directory.iterdir():
            if MASK.fullmatch(path.name):
                data=path.read_bytes()
                total+=len(data)
                if len(data)>4*MIB or total>16*MIB:
                    raise MemoryError("Helper output exceeds transport budget.")
                files[path.name]=base64.b64encode(data).decode("ascii")
        if callable(should_stop) and should_stop():
            raise InterruptedError("O usuário interrompeu o auxiliar.")
        if callable(on_progress):
            on_progress("Enviando análise ao servidor 3DX…")
        request(token,"/api/v1/worker/complete",
                {"job_id":task["job_id"],"lease":task["lease"],"report":report,"files":files})
        print("✓ Imagens analisadas no Windows; modelo enviado à fila Blender do Render.")


def main():
    if sys.version_info < (3,11):
        print("Instale Python 3.11 ou posterior, versão 64 bits.")
        return 2
    try:
        import numpy
        import PIL
    except ImportError:
        print("Faltam bibliotecas gratuitas. Execute: py -3 -m pip install --user numpy pillow")
        return 2
    print("3DX HYBRID LITE — computador auxiliar (sem Blender)")
    print("Mesmo laboratório:", ORIGIN+"/studio")
    print("Deixe esta janela aberta; Ctrl+C encerra. Nenhuma porta será aberta.")
    token=getpass.getpass("Chave de acesso 3DX (não aparece na tela): ").strip()
    if not token:
        print("Chave vazia; encerrando.")
        return 2
    cache=Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())/"3DXHybridLite"/"pipeline"
    try:
        while True:
            free=available_ram()
            if free is not None and free<950*MIB:
                print("Pouca RAM disponível (menos de 950 MB). Aguardando...")
                time.sleep(20)
                continue
            try:
                response=request(token,"/api/v1/worker/claim",{})
                task=response.get("task")
                if task:
                    print("Tarefa recebida. Analisando imagens no computador...")
                    try:
                        analyze(token, task,cache)
                    except Exception as exc:
                        print("Análise local indisponível:",type(exc).__name__,str(exc)[:110])
                        try:
                            request(token,"/api/v1/worker/abandon",
                                    {"job_id":task["job_id"],"lease":task["lease"]})
                            print("✓ Tarefa devolvida ao Render.")
                        except Exception:
                            print("O Render recuperará a tarefa após o prazo de segurança.")
                else:
                    time.sleep(IDLE_SECONDS)
            except HTTPError as exc:
                if exc.code in (401,403):
                    print("Chave inválida ou sem autorização. Encerrando.")
                    return 3
                print("Render temporariamente indisponível (HTTP %s)." % exc.code)
                time.sleep(18)
            except (URLError,TimeoutError,OSError,ValueError) as exc:
                print("Conexão indisponível; nova tentativa em alguns segundos.")
                time.sleep(18)
    except KeyboardInterrupt:
        print("\n3DX Hybrid Lite encerrado.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
