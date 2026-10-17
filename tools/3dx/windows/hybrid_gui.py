"""3DX Hybrid Lite 2 — Windows GUI (Tkinter, stdlib only).

The existing authenticated 3DX Render lease and Blender contracts are reused.
No new lab, endpoint, or game build is created. The GUI never stores the key.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import webbrowser

import hybrid_lite as engine

ORIGIN = "https://threedx-studio-blender-qa.onrender.com"
LAB_URL = ORIGIN + "/studio"
BG = "#0D1420"
PANEL = "#172335"
INNER = "#101B2C"
LINE = "#2E3E55"
TEXT = "#EAF1FA"
MUTED = "#A0B5CD"
CYAN = "#3BE4C3"
BLUE = "#65B8FF"
AMBER = "#F9C572"
RED = "#FF828E"
MB = 1024 * 1024


def server_error(exc: HTTPError) -> str:
    message = ""
    try:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict):
            message = str(data.get("error") or data.get("message") or data.get("code") or "")
        else:
            message = raw[:200]
    except (OSError, ValueError, TypeError):
        message = ""
    hints = {
        400: "Requisição rejeitada pela API. Confira a resposta; não significa que o Render caiu.",
        401: "A chave foi recusada. Confira o token usado no Studio.",
        403: "Acesso negado pelo servidor.",
        404: "Rota de comunicação ausente nesta versão do servidor.",
        409: "Versões incompatíveis: atualize o auxiliar.",
        413: "Arquivos acima do limite do servidor.",
        422: "Dados da tarefa foram recusados.",
        429: "Servidor ocupado; aguarde a próxima tentativa.",
        503: "Serviço indisponível ou token não configurado no Render.",
    }
    suffix = " — " + message[:260] if message else ""
    return f"HTTP {exc.code}: {hints.get(exc.code, 'Falha de comunicação.')}{suffix}"


def check_access(token: str) -> None:
    """Protected read-only catalog is safe: does not take a processing lease."""
    request = Request(
        ORIGIN + "/api/v1/catalog",
        headers={"Authorization": "Bearer " + token,
                 "X-3DX-Asset-Contract": "3dx-asset-package/1",
                 "Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=28) as response:
        if response.status != 200:
            raise RuntimeError("Resposta inesperada do laboratório.")
        payload = json.loads(response.read(1024 * 1024))
        if not isinstance(payload, dict) or "characters" not in payload:
            raise RuntimeError("O servidor não retornou o catálogo esperado.")


class App:
    def __init__(self):
        root = tk.Tk()
        self.root = root
        root.title("3DX Hybrid Lite 2 — Auxiliar Windows")
        root.geometry("840x680")
        root.minsize(700, 575)
        root.configure(bg=BG)
        try:
            root.iconbitmap(default="")
        except (tk.TclError, OSError):
            pass
        self.active = False
        self.worker: threading.Thread | None = None
        self.stop = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.last_wait = ""
        self.token = ""
        self.cache = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "3DXHybridLite" / "pipeline"

        self.secret = tk.StringVar()
        self.status = tk.StringVar(value="Desconectado")
        self.subtitle = tk.StringVar(value="Conecte a chave do 3DX para começar.")
        self.mem_label = tk.StringVar(value="Consultando RAM…")
        self.eco = tk.BooleanVar(value=False)
        self.show_password = tk.BooleanVar(value=False)
        self._build()
        self._sync_controls()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(250, self._drain)
        root.after(500, self._poll_memory)

    def _label(self, parent, text, size=11, color=TEXT, weight="normal", **kwargs):
        return tk.Label(parent, text=text, fg=color, bg=parent.cget("bg"),
                        font=("Segoe UI", size, weight), **kwargs)

    def _panel(self, parent, padding=18):
        frame = tk.Frame(parent, bg=PANEL, padx=padding, pady=padding,
                         highlightthickness=1, highlightbackground=LINE)
        return frame

    def _build(self):
        base = tk.Frame(self.root, bg=BG, padx=23, pady=18)
        base.pack(fill="both", expand=True)

        top = tk.Frame(base, bg=BG)
        top.pack(fill="x", pady=(0, 15))
        self._label(top, "3DX", 26, CYAN, "bold").pack(side="left")
        self._label(top, "  HYBRID LITE", 22, TEXT, "bold").pack(side="left")
        self._label(top, "WINDOWS  •  V2", 10, MUTED, "bold").pack(side="right")

        overview = self._panel(base)
        overview.pack(fill="x", pady=(0, 12))
        self._label(overview, "Seu PC ajuda o 3DX a analisar referências", 14, TEXT, "bold").pack(anchor="w")
        self._label(overview,
                    "O Blender e os arquivos GLB continuam no mesmo Render oficial. "
                    "A RAM do computador não é somada à RAM do servidor.",
                    10, MUTED, wraplength=735, justify="left").pack(anchor="w", pady=(7, 0))

        auth = self._panel(base)
        auth.pack(fill="x", pady=(0, 12))
        self._label(auth, "ACESSO AO LABORATÓRIO", 10, BLUE, "bold").pack(anchor="w", pady=(0, 9))
        secret_row = tk.Frame(auth, bg=PANEL)
        secret_row.pack(fill="x")
        self.password = tk.Entry(
            secret_row, textvariable=self.secret, show="●", bg=INNER, fg=TEXT,
            insertbackground=CYAN, relief="flat", font=("Segoe UI", 12),
            highlightthickness=1, highlightcolor=CYAN, highlightbackground=LINE
        )
        self.password.pack(side="left", fill="x", expand=True, ipady=9)
        self.password.bind("<Return>", lambda _: self.connect())
        self.eye = tk.Checkbutton(
            secret_row, text="Mostrar", variable=self.show_password, bg=PANEL,
            fg=MUTED, activebackground=PANEL, activeforeground=CYAN,
            selectcolor=INNER, command=self._toggle_password,
            font=("Segoe UI", 10)
        )
        self.eye.pack(side="left", padx=(10, 0))
        buttons = tk.Frame(auth, bg=PANEL)
        buttons.pack(fill="x", pady=(12, 0))
        self.connect_btn = self._button(buttons, "Conectar", self.connect, CYAN, BG)
        self.connect_btn.pack(side="left", padx=(0, 8))
        self.test_btn = self._button(buttons, "Testar conexão", self.test_connection, INNER, TEXT)
        self.test_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = self._button(buttons, "Desconectar", self.disconnect, INNER, TEXT)
        self.stop_btn.pack(side="left")
        self._button(buttons, "Abrir laboratório ↗",
                     lambda: webbrowser.open(LAB_URL), PANEL, BLUE).pack(side="right")

        resources = self._panel(base)
        resources.pack(fill="x", pady=(0, 12))
        title_row = tk.Frame(resources, bg=PANEL)
        title_row.pack(fill="x")
        self._label(title_row, "MEMÓRIA DO COMPUTADOR", 10, BLUE, "bold").pack(side="left")
        self._label(title_row, "", 10, MUTED, textvariable=self.mem_label).pack(side="right")
        self.gauge = tk.Canvas(resources, height=14, bg=INNER, highlightthickness=0)
        self.gauge.pack(fill="x", pady=(10, 8))
        self.gauge.bind("<Configure>", lambda _: self._draw_ram())
        self.ram_free = None
        self.eco_checkbox = tk.Checkbutton(
            resources, text="Modo memória reduzida (experimental: mínimo de 750 MB livres)",
            variable=self.eco, bg=PANEL, fg=TEXT, activebackground=PANEL,
            activeforeground=CYAN, selectcolor=INNER,
            font=("Segoe UI", 10), command=self._poll_memory,
        )
        self.eco_checkbox.pack(anchor="w")
        self._label(resources, "A análise para automaticamente se a RAM cair. "
                    "Feche abas e aplicativos pesados para liberar memória.",
                    9, MUTED, wraplength=735, justify="left").pack(anchor="w", pady=(5, 0))

        activity = self._panel(base, padding=14)
        activity.pack(fill="both", expand=True)
        head = tk.Frame(activity, bg=PANEL)
        head.pack(fill="x")
        self._label(head, "ATIVIDADE E DIAGNÓSTICO", 10, BLUE, "bold").pack(side="left")
        self._button(head, "Limpar", self.clear_log, INNER, TEXT).pack(side="right")
        self.status_label = tk.Label(
            activity, textvariable=self.status, fg=CYAN, bg=PANEL,
            font=("Segoe UI", 14, "bold"), anchor="w"
        )
        self.status_label.pack(anchor="w", fill="x", pady=(5, 0))
        self._label(activity, "", 10, MUTED, textvariable=self.subtitle,
                    wraplength=735, justify="left").pack(anchor="w", pady=(0, 8))
        self.console = tk.Text(
            activity, height=7, bg=INNER, fg=MUTED, relief="flat",
            font=("Consolas", 10), wrap="word", state="disabled",
            highlightthickness=1, highlightbackground=LINE
        )
        self.console.pack(fill="both", expand=True)
        self._log("Pronto. Digite a chave do mesmo 3DX Studio. Ela não fica salva em disco.")

    def _button(self, parent, title, command, bg, fg):
        return tk.Button(parent, text=title, command=command, bg=bg, fg=fg,
                         activebackground=BLUE, activeforeground=BG, relief="flat",
                         padx=14, pady=7, cursor="hand2", font=("Segoe UI", 10, "bold"),
                         disabledforeground="#5E7085", bd=0)

    def _toggle_password(self):
        self.password.configure(show="" if self.show_password.get() else "●")

    def _log(self, message):
        self.console.configure(state="normal")
        self.console.insert("end", datetime.now().strftime("%H:%M:%S") + "  " + message + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def clear_log(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _status(self, title, subtitle, color=CYAN):
        self.status.set(title)
        self.subtitle.set(subtitle)
        self.status_label.configure(fg=color)

    def _emit(self, typ, text):
        self.events.put((typ, text))

    def _drain(self):
        try:
            while True:
                typ, content = self.events.get_nowait()
                if typ == "log":
                    self._log(content)
                elif typ == "status":
                    title, detail, col = content
                    self._status(title, detail, col)
                elif typ == "done":
                    self.active = False
                    self._sync_controls()
                elif typ == "test_done":
                    self.test_btn.configure(state="disabled" if self.active else "normal")
        except queue.Empty:
            pass
        self.root.after(220, self._drain)

    def _draw_ram(self):
        if not self.gauge.winfo_exists():
            return
        width = max(2, self.gauge.winfo_width())
        self.gauge.delete("all")
        self.gauge.create_rectangle(0, 0, width, 14, fill=INNER, outline="")
        free = self.ram_free
        if free is None:
            return
        threshold = 750 if self.eco.get() else 950
        color = CYAN if free >= threshold else RED
        self.gauge.create_rectangle(0, 0, min(width, width * min(1, free / 1800)),
                                    14, fill=color, outline="")
        self.gauge.create_line(width * threshold / 1800, 0,
                               width * threshold / 1800, 14, fill=AMBER, width=2)

    def _poll_memory(self):
        raw = engine.available_ram()
        self.ram_free = None if raw is None else raw // MB
        if self.ram_free is None:
            self.mem_label.set("Verificação disponível no Windows")
        else:
            target = 750 if self.eco.get() else 950
            self.mem_label.set(f"{self.ram_free:,} MB livres  •  mínimo {target} MB".replace(",", "."))
        self._draw_ram()
        self.root.after(2000, self._poll_memory)

    def _sync_controls(self):
        self.connect_btn.configure(state="disabled" if self.active else "normal")
        self.stop_btn.configure(state="normal" if self.active else "disabled")
        self.test_btn.configure(state="disabled" if self.active else "normal")
        self.eco_checkbox.configure(state="disabled" if self.active else "normal")

    def _token(self):
        value = self.secret.get().strip()
        if len(value) < 8 or "\n" in value:
            self._status("Chave necessária", "Cole sua chave de acesso do laboratório 3DX.", AMBER)
            return None
        return value

    def test_connection(self):
        token = self._token()
        if not token:
            return
        self.test_btn.configure(state="disabled")
        self._status("Verificando...", "Teste somente leitura; nenhuma tarefa será retirada da fila.", BLUE)
        def check():
            try:
                check_access(token)
                self._emit("log", "Conexão e chave aceitas pelo laboratório oficial.")
                self._emit("status", ("Conexão autorizada", "Pode conectar para receber tarefas.", CYAN))
            except HTTPError as e:
                message = server_error(e)
                self._emit("log", message)
                self._emit("status", ("Erro ao autenticar", message, RED))
            except Exception as e:
                self._emit("log", "Falha no teste: " + str(e)[:220])
                self._emit("status", ("Sem conexão", "Verifique sua internet e o estado do Render.", AMBER))
            finally:
                self._emit("test_done", None)
        threading.Thread(target=check, name="3DX-connection-test", daemon=True).start()

    def connect(self):
        if self.active:
            return
        token = self._token()
        if not token:
            return
        self.active = True
        self.stop.clear()
        self.secret.set("")
        self.password.configure(show="●")
        self.show_password.set(False)
        threshold = 750 if self.eco.get() else 950
        stop_floor = 520 if self.eco.get() else 650
        self._sync_controls()
        self._status("Conectando ao 3DX…", "Verificando chave de acesso e catálogo.", BLUE)
        self._log("Iniciando auxiliar com proteção de RAM e fallback automático ao Render.")
        self.worker = threading.Thread(target=self._loop,
                                       args=(token, threshold, stop_floor),
                                       name="3DX-windows-worker", daemon=True)
        self.worker.start()

    def disconnect(self):
        self.stop.set()
        self._status("Desconectando…", "Aguardando interrupção segura da tarefa atual.", AMBER)

    def _loop(self, token, threshold, stop_floor):
        try:
            try:
                check_access(token)
                self._emit("log", "Chave validada; comunicação HTTPS autorizada.")
            except HTTPError as e:
                self._emit("log", server_error(e))
                self._emit("status", ("Acesso recusado", server_error(e), RED))
                return
            except Exception as e:
                self._emit("log", "Falha na checagem da API: " + str(e)[:200])
                self._emit("status", ("Laboratório inacessível", "Verifique rede ou Render.", RED))
                return
            while not self.stop.is_set():
                free = engine.available_ram()
                if free is not None and free < threshold * MB:
                    status = f"RAM baixa: {free//MB} MB livres (mínimo {threshold} MB)."
                    if status != self.last_wait:
                        self._emit("log", status + " Feche aplicativos para liberar RAM.")
                        self.last_wait = status
                    self._emit("status", ("Aguardando RAM", "Sem consumir processamento pesado.", AMBER))
                    self.stop.wait(10)
                    continue
                self.last_wait = ""
                task = None
                completed = False
                try:
                    self._emit("status", ("Conectado", "Procurando tarefas do 3DX oficial…", CYAN))
                    response = engine.request(token, "/api/v1/worker/claim", {})
                    if not isinstance(response, dict):
                        raise ValueError("Resposta do 3DX não é objeto JSON.")
                    task = response.get("task")
                    if task is None:
                        self.stop.wait(14)
                        continue
                    self._emit("log", "Tarefa " + str(task.get("job_id", ""))[:8] +
                               " recebida. Processando referências…")
                    engine.analyze(
                        token, task, self.cache, should_stop=self.stop.is_set,
                        start_free_mb=threshold, stop_free_mb=stop_floor,
                        on_progress=lambda msg: self._emit("status", ("Processando no Windows", msg, BLUE))
                    )
                    completed = True
                    self._emit("log", "Resultado integrado à fila oficial do Render.")
                except HTTPError as e:
                    msg = server_error(e)
                    self._emit("log", msg)
                    self._emit("status", ("Erro de API", msg, RED))
                    if e.code in (400, 401, 403, 404, 409, 422):
                        return
                    self.stop.wait(20)
                except Exception as e:
                    self._emit("log", "Processamento: " + type(e).__name__ + " — " + str(e)[:170])
                    self._emit("status", ("Recuperação automática", "Retornando trabalho ao Render.", AMBER))
                    self.stop.wait(16)
                finally:
                    if isinstance(task, dict) and self.stop.is_set():
                        self._emit("log", "Conexão encerrada; tarefa não concluída volta ao Render.")
                    if isinstance(task, dict) and not completed:
                        # Release a valid lease immediately instead of waiting 10 minutes.
                        try:
                            engine.request(token, "/api/v1/worker/abandon",
                                           {"job_id": task["job_id"], "lease": task["lease"]})
                        except Exception:
                            pass
        finally:
            self._emit("done", None)
            if self.stop.is_set():
                self._emit("status", ("Desconectado", "O auxiliar Windows foi parado.", MUTED))

    def close(self):
        self.stop.set()
        self.root.destroy()


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ necessário.")
    app = App()
    app.root.mainloop()


if __name__ == "__main__":
    main()
