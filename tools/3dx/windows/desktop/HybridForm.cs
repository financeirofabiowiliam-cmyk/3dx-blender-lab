using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Net;
using System.Net.Http.Headers;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace AngelsNight.ThreeDX;

public sealed class HybridForm : Form
{
    private const string Lab = "https://threedx-studio-blender-qa.onrender.com/studio";
    private const string Host = "https://threedx-studio-blender-qa.onrender.com";
    private readonly Color bg = Color.FromArgb(13, 20, 34);
    private readonly Color panel = Color.FromArgb(22, 34, 51);
    private readonly Color light = Color.FromArgb(236, 242, 250);
    private readonly Color muted = Color.FromArgb(159, 180, 204);
    private readonly Color mint = Color.FromArgb(53, 222, 184);
    private readonly Color blue = Color.FromArgb(103, 185, 255);
    private readonly TabControl tabs = new();
    private readonly TabPage labTab = new("Laboratório 3D");
    private readonly TabPage workerTab = new("Conexão e recursos");
    private readonly Panel labSurface = new();
    private WebView2? browser;
    private bool loadingLab;
    private readonly TextBox token = new();
    private readonly Label state = new();
    private readonly Label hint = new();
    private readonly Label memory = new();
    private readonly Label cpu = new();
    private readonly TextBox log = new();
    private readonly Panel graph = new();
    private readonly ComboBox profile = new();
    private readonly Button start = new();
    private readonly Button stop = new();
    private readonly Button test = new();
    private readonly CheckBox autoRelease = new();
    private readonly List<float> ramSamples = new();
    private readonly List<float> cpuSamples = new();
    private readonly System.Windows.Forms.Timer tick = new();
    private Process? worker;
    private DateTime lastCpuAt;
    private TimeSpan lastCpu;
    private bool closing;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
    private class MemoryStatus
    {
        public uint Length = (uint)Marshal.SizeOf<MemoryStatus>();
        public uint Load;
        public ulong TotalPhys;
        public ulong AvailPhys;
        public ulong TotalPageFile;
        public ulong AvailPageFile;
        public ulong TotalVirtual;
        public ulong AvailVirtual;
        public ulong AvailExtendedVirtual;
    }
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GlobalMemoryStatusEx([In, Out] MemoryStatus status);

    public HybridForm()
    {
        Text = "3DX Hybrid Desktop — Angels Night World";
        Size = new Size(1200, 775);
        MinimumSize = new Size(920, 620);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = bg;
        ForeColor = light;
        Font = new Font("Segoe UI", 10);
        Icon = SystemIcons.Application;
        Controls.Add(CreateHeader());
        tabs.Dock = DockStyle.Fill;
        tabs.Font = new Font("Segoe UI", 11, FontStyle.Bold);
        tabs.Appearance = TabAppearance.Normal;
        tabs.TabPages.Add(labTab);
        tabs.TabPages.Add(workerTab);
        Controls.Add(tabs);
        tabs.BringToFront();
        BuildLab();
        BuildWorker();
        tabs.SelectedIndexChanged += (_, _) => {
            if (tabs.SelectedTab == workerTab && autoRelease.Checked)
                ReleaseLab();
            else if (tabs.SelectedTab == labTab)
                _ = LoadLabAsync();
        };
        Shown += (_, _) => _ = LoadLabAsync();
        tick.Interval = 1400;
        tick.Tick += (_, _) => Measure();
        tick.Start();
        FormClosing += (_, _) => { closing = true; tick.Stop(); StopWorker(); ReleaseLab(); };
        UpdateControls();
    }

    private Control CreateHeader()
    {
        var p = new Panel { Dock = DockStyle.Top, Height = 58, BackColor = bg, Padding = new Padding(18, 8, 18, 6) };
        var brand = NewLabel("3DX  /  HYBRID DESKTOP", 19, mint, FontStyle.Bold);
        brand.Dock = DockStyle.Left;
        brand.Width = 340;
        var desc = NewLabel("Mesmo laboratório oficial • Android + Windows + Render", 10, muted);
        desc.Dock = DockStyle.Right;
        desc.Width = 470;
        desc.TextAlign = ContentAlignment.MiddleRight;
        p.Controls.Add(brand); p.Controls.Add(desc);
        return p;
    }

    private static Label NewLabel(string text, int points, Color color, FontStyle weight = FontStyle.Regular)
        => new() { Text = text, ForeColor = color, AutoSize = false, Height = 34,
                   Font = new Font("Segoe UI", points, weight),
                   TextAlign = ContentAlignment.MiddleLeft, BackColor = Color.Transparent };

    private Button NewButton(string text, EventHandler action, bool emphasized = false)
    {
        var b = new Button {
            Text = text, FlatStyle = FlatStyle.Flat, Width = 155, Height = 38, Margin = new Padding(0, 0, 10, 0),
            BackColor = emphasized ? mint : Color.FromArgb(32, 47, 69),
            ForeColor = emphasized ? bg : light,
            Font = new Font("Segoe UI", 10, FontStyle.Bold),
            Cursor = Cursors.Hand
        };
        b.FlatAppearance.BorderSize = 0; b.Click += action;
        return b;
    }

    private void BuildLab()
    {
        labTab.BackColor = bg;
        var top = new Panel { Dock = DockStyle.Top, Height = 74, BackColor = panel, Padding = new Padding(16, 9, 16, 8) };
        var row = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 43, WrapContents = false, BackColor = panel };
        row.Controls.Add(NewButton("Recarregar 3DX", async (_, _) => await LoadLabAsync(true), true));
        row.Controls.Add(NewButton("Liberar RAM da aba", (_, _) => ReleaseLab()));
        autoRelease.Text = "Liberar RAM ao sair da aba";
        autoRelease.ForeColor = light; autoRelease.BackColor = panel;
        autoRelease.Checked = true; autoRelease.AutoSize = true;
        autoRelease.Padding = new Padding(12, 9, 0, 0);
        row.Controls.Add(autoRelease);
        top.Controls.Add(row);
        var msg = NewLabel("O laboratório abaixo é integrado por WebView2, não um atalho ou uma janela do Chrome.", 9, muted);
        msg.Dock = DockStyle.Bottom; msg.Height = 22;
        top.Controls.Add(msg);
        labSurface.Dock = DockStyle.Fill;
        labSurface.BackColor = Color.FromArgb(16, 26, 40);
        labTab.Controls.Add(labSurface);
        labTab.Controls.Add(top);
    }

    private async Task LoadLabAsync(bool force = false)
    {
        if (closing || loadingLab) return;
        if (!force && browser != null && browser.CoreWebView2 != null) return;
        loadingLab = true;
        try
        {
            if (force) ReleaseLab();
            var view = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.FromArgb(16, 26, 40) };
            browser = view;
            labSurface.Controls.Add(view);
            var folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                      "3DXHybridLite", "WebView2");
            Directory.CreateDirectory(folder);
            var environment = await CoreWebView2Environment.CreateAsync(null, folder);
            if (closing || browser != view) { view.Dispose(); return; }
            await view.EnsureCoreWebView2Async(environment);
            view.CoreWebView2.Settings.AreDevToolsEnabled = false;
            view.CoreWebView2.Settings.IsStatusBarEnabled = false;
            view.CoreWebView2.NavigationCompleted += (_, args) => {
                if (!closing && !args.IsSuccess) AddLog("Laboratório WebView2: navegação falhou. Código " + args.WebErrorStatus);
            };
            view.Source = new Uri(Lab);
            AddLog("Laboratório oficial aberto dentro do programa (WebView2).");
        }
        catch (Exception exc)
        {
            AddLog("Erro ao abrir laboratório integrado: " + exc.Message);
            var alert = NewLabel("WebView2 não disponível. Instale o Microsoft Edge WebView2 Runtime e recarregue a aba.", 12, muted);
            alert.Dock = DockStyle.Top; alert.Height = 60;
            labSurface.Controls.Add(alert);
        }
        finally { loadingLab = false; }
    }

    private void ReleaseLab()
    {
        try {
            if (browser != null) { labSurface.Controls.Remove(browser); browser.Dispose(); browser = null; }
        }
        catch (ObjectDisposedException) { browser = null; }
        if (!closing) AddLog("Visualizador 3D descarregado para liberar memória; tarefas no Render continuam.");
    }

    private void BuildWorker()
    {
        workerTab.BackColor = bg;
        var scroll = new Panel { Dock = DockStyle.Fill, AutoScroll = true, Padding = new Padding(22, 15, 22, 10), BackColor = bg };
        workerTab.Controls.Add(scroll);
        var content = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, RowCount = 5, BackColor = bg };
        content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        scroll.Controls.Add(content);

        var expl = NewPanel(82);
        expl.Controls.Add(NewLabel("1. CONECTAR OS DISPOSITIVOS", 12, blue, FontStyle.Bold));
        var desc = NewLabel("Esta aba controla o Windows. No Laboratório, marque Android e Windows juntos. "
                            + "O celular prepara as fotos, o PC analisa e o Render gera o GLB.", 10, muted);
        desc.SetBounds(0, 37, 900, 40);
        expl.Controls.Add(desc); content.Controls.Add(expl);

        var access = NewPanel(146);
        var title = NewLabel("2. CHAVE DO LABORATÓRIO", 11, blue, FontStyle.Bold);
        title.SetBounds(0, 0, 330, 31); access.Controls.Add(title);
        token.UseSystemPasswordChar = true;
        token.PlaceholderText = "Cole a mesma chave usada no 3DX";
        token.BackColor = Color.FromArgb(13, 21, 34); token.ForeColor = light;
        token.BorderStyle = BorderStyle.FixedSingle;
        token.Font = new Font("Segoe UI", 11);
        token.SetBounds(0, 40, 600, 31);
        token.Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Top;
        access.Controls.Add(token);
        var buttons = new FlowLayoutPanel { Left = 0, Top = 85, Height = 43, Width = 790, WrapContents = false };
        test.Text = "Testar chave"; start.Text = "Conectar PC"; stop.Text = "Desconectar";
        ConfigureButton(test, (_, _) => _ = TestConnectionAsync(), false);
        ConfigureButton(start, (_, _) => ConnectWorker(), true);
        ConfigureButton(stop, (_, _) => StopWorker(), false);
        buttons.Controls.Add(test); buttons.Controls.Add(start); buttons.Controls.Add(stop);
        access.Controls.Add(buttons); content.Controls.Add(access);

        var resources = NewPanel(182);
        resources.Controls.Add(NewLabel("3. MEMÓRIA E PROCESSAMENTO AO VIVO", 11, blue, FontStyle.Bold));
        memory.SetBounds(0, 34, 640, 27); memory.ForeColor = light;
        cpu.SetBounds(0, 62, 640, 23); cpu.ForeColor = muted;
        resources.Controls.Add(memory); resources.Controls.Add(cpu);
        graph.SetBounds(0, 92, 780, 69);
        graph.Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Top;
        graph.BackColor = Color.FromArgb(12, 23, 37);
        graph.Paint += (_, e) => PaintGraph(e.Graphics, graph.ClientRectangle);
        resources.Controls.Add(graph); content.Controls.Add(resources);

        var modes = NewPanel(95);
        modes.Controls.Add(NewLabel("4. INTENSIDADE DO AUXILIAR", 11, blue, FontStyle.Bold));
        profile.DropDownStyle = ComboBoxStyle.DropDownList;
        profile.Items.Add("Equilibrado • iniciar com 950 MB livres");
        profile.Items.Add("Intensivo • iniciar com 650 MB livres");
        profile.SelectedIndex = 0;
        profile.BackColor = Color.FromArgb(22, 34, 51); profile.ForeColor = light; profile.FlatStyle = FlatStyle.Flat;
        profile.Font = new Font("Segoe UI", 11);
        profile.SetBounds(0, 39, 520, 32);
        modes.Controls.Add(profile); content.Controls.Add(modes);

        var logs = NewPanel(156);
        state.SetBounds(0, 0, 785, 27); state.ForeColor = mint;
        state.Font = new Font("Segoe UI", 12, FontStyle.Bold); state.Text = "Desconectado";
        hint.SetBounds(0, 30, 790, 30); hint.ForeColor = muted;
        hint.Text = "Você pode gerar personagens na primeira aba enquanto esta conexão permanece ativa.";
        log.Multiline = true; log.ReadOnly = true; log.BorderStyle = BorderStyle.None;
        log.ScrollBars = ScrollBars.Vertical; log.BackColor = Color.FromArgb(11, 21, 32);
        log.ForeColor = muted; log.Font = new Font("Consolas", 9);
        log.SetBounds(0, 66, 780, 76); log.Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Top;
        logs.Controls.Add(state); logs.Controls.Add(hint); logs.Controls.Add(log);
        content.Controls.Add(logs);
        AddLog("Interface pronta. A chave não é salva; o processamento é limitado pela RAM física disponível.");
        content.SizeChanged += (_,_) => {
            int width = Math.Max(630, content.ClientSize.Width - 42);
            foreach (Control c in content.Controls) { c.Width = width; }
            token.Width = Math.Max(380, width - 28);
            graph.Width = Math.Max(380, width - 28);
            log.Width = Math.Max(380, width - 28);
        };
    }

    private Panel NewPanel(int height) => new() {
        BackColor = panel, Height = height, Width = 930, Padding = new Padding(15),
        Margin = new Padding(0, 0, 0, 12),
    };

    private void ConfigureButton(Button button, EventHandler click, bool accented)
    {
        button.Width = 160; button.Height = 38;
        button.BackColor = accented ? mint : Color.FromArgb(37, 54, 75);
        button.ForeColor = accented ? bg : light;
        button.FlatStyle = FlatStyle.Flat; button.FlatAppearance.BorderSize = 0;
        button.Font = new Font("Segoe UI", 10, FontStyle.Bold);
        button.Click += click; button.Margin = new Padding(0, 0, 8, 0);
    }

    private void UpdateControls()
    {
        bool active = worker != null && !worker.HasExited;
        start.Enabled = !active;
        test.Enabled = !active;
        stop.Enabled = active;
        profile.Enabled = !active;
    }

    private async Task TestConnectionAsync()
    {
        string key = token.Text.Trim();
        if (key.Length < 8) { state.Text = "Insira uma chave do 3DX para testar."; return; }
        test.Enabled = false;
        state.Text = "Testando chave com o mesmo Render...";
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(25) };
            using var request = new HttpRequestMessage(HttpMethod.Get, Host + "/api/v1/catalog");
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", key);
            using var reply = await http.SendAsync(request);
            if (!reply.IsSuccessStatusCode) {
                string details = await reply.Content.ReadAsStringAsync();
                throw new Exception("HTTP " + (int)reply.StatusCode + " — " +
                                    details[..Math.Min(220, details.Length)]);
            }
            AddLog("Chave e laboratório confirmados. Nenhuma tarefa foi retirada da fila.");
            state.Text = "Chave autorizada. Pronto para conectar.";
        }
        catch (Exception e) { state.Text = "Conexão falhou"; AddLog("Teste: " + e.Message); }
        finally { UpdateControls(); }
    }

    private void ConnectWorker()
    {
        if (worker is { HasExited: false }) return;
        string key = token.Text.Trim();
        if (key.Length < 8) { state.Text = "Insira a chave de acesso antes de conectar."; return; }
        var home = AppContext.BaseDirectory;
        var script = Path.Combine(home, "hybrid_lite.py");
        if (!File.Exists(script)) { AddLog("hybrid_lite.py não encontrado ao lado do executável."); return; }
        int startMb = profile.SelectedIndex == 1 ? 650 : 950;
        int stopMb = profile.SelectedIndex == 1 ? 400 : 650;
        try
        {
            var info = new ProcessStartInfo("py") {
                Arguments = "-3 -u \"" + script + "\" --service-stdin --start-free=" + startMb + " --stop-free=" + stopMb,
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = home,
            };
            worker = Process.Start(info);
            if (worker == null) throw new InvalidOperationException("Não foi possível iniciar Python.");
            worker.OutputDataReceived += (_, e) => { if (e.Data != null) Ui(() => ProcessOutput(e.Data)); };
            worker.ErrorDataReceived += (_, e) => { if (e.Data != null) Ui(() => ProcessOutput(e.Data)); };
            worker.EnableRaisingEvents = true;
            worker.Exited += (_, _) => Ui(() => {
                state.Text = "Auxiliar Windows desconectado";
                AddLog("O processo auxiliar terminou. O Render retoma tarefas não concluídas.");
                worker?.Dispose();
                worker = null; UpdateControls();
            });
            worker.BeginOutputReadLine(); worker.BeginErrorReadLine();
            worker.StandardInput.WriteLine(key);
            worker.StandardInput.Flush();
            token.Clear();
            state.Text = "Conectado: verificando RAM e procurando tarefas";
            hint.Text = "Windows e Android podem participar juntos. Esta aba mostra o processamento local.";
            AddLog("Processamento iniciado no PC: proteção de RAM " + startMb + " / " + stopMb + " MB.");
            UpdateControls();
        }
        catch (Exception e) { state.Text = "Falha ao iniciar"; AddLog("Instale Python 3.11 e dependências: " + e.Message); }
    }

    private void StopWorker()
    {
        if (worker == null) return;
        try { if (!worker.HasExited) worker.Kill(entireProcessTree: true); }
        catch (InvalidOperationException) { }
        catch (System.ComponentModel.Win32Exception) { }
        if (!closing) { state.Text = "Desconectando..."; AddLog("Desconexão solicitada. O servidor recuperará tarefas inacabadas."); }
    }

    private void ProcessOutput(string line)
    {
        if (string.IsNullOrWhiteSpace(line)) return;
        string safe = line.Trim().Length > 240 ? line.Trim()[..240] : line.Trim();
        // The password must not be echoed by the managed Python worker.
        AddLog(safe);
        if (safe.Contains("Aguardando RAM", StringComparison.OrdinalIgnoreCase))
            state.Text = "Aguardando memória livre no Windows";
        else if (safe.Contains("Tarefa recebida", StringComparison.OrdinalIgnoreCase))
            state.Text = "Processando fotografias";
        else if (safe.Contains("envia", StringComparison.OrdinalIgnoreCase))
            state.Text = "Enviando trabalho para o Render";
        else if (safe.Contains("Falha HTTP", StringComparison.OrdinalIgnoreCase))
            state.Text = "Erro de comunicação — detalhes abaixo";
        else if (safe.Contains("fila Blender", StringComparison.OrdinalIgnoreCase))
            state.Text = "Análise pronta! Blender continua no Render.";
    }

    private void AddLog(string message)
    {
        if (closing || log.IsDisposed) return;
        if (log.TextLength > 20000) log.Text = log.Text[^10000..];
        log.AppendText(DateTime.Now.ToString("HH:mm:ss") + "  " + message + Environment.NewLine);
    }

    private void Ui(Action action)
    {
        try { if (!closing && IsHandleCreated) BeginInvoke(action); }
        catch (InvalidOperationException) { }
        catch (ObjectDisposedException) { }
    }

    private void Measure()
    {
        var mem = new MemoryStatus();
        if (GlobalMemoryStatusEx(mem) && mem.TotalPhys > 0) {
            var freeMb = mem.AvailPhys / 1048576.0;
            memory.Text = "RAM livre: " + freeMb.ToString("N0") + " MB  |  total: " +
                          (mem.TotalPhys / 1048576.0).ToString("N0") + " MB";
            ramSamples.Add((float)(mem.AvailPhys / (double)mem.TotalPhys));
            if (ramSamples.Count > 80) ramSamples.RemoveAt(0);
        }
        var p = worker;
        float load = 0;
        if (p is { HasExited: false }) {
            try {
                var now = DateTime.UtcNow;
                var used = p.TotalProcessorTime;
                if (lastCpuAt != default) {
                    var delta = now - lastCpuAt;
                    if (delta.TotalMilliseconds > 0)
                        load = (float)Math.Clamp((used - lastCpu).TotalMilliseconds /
                               (delta.TotalMilliseconds * Environment.ProcessorCount), 0.0, 1.0);
                }
                lastCpu = used; lastCpuAt = now;
            } catch (InvalidOperationException) { }
        } else lastCpuAt = default;
        cpu.Text = "CPU do auxiliar: " + (load * 100).ToString("F1") +
                   "%  |  gráfico da RAM livre (verde) e CPU auxiliar (azul)";
        cpuSamples.Add(load);
        if (cpuSamples.Count > 80) cpuSamples.RemoveAt(0);
        graph.Invalidate();
    }

    private void PaintGraph(Graphics g, Rectangle area)
    {
        g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
        using var grid = new Pen(Color.FromArgb(48, 70, 89));
        for (int i = 1; i < 4; i++) {
            float y = area.Height * i / 4f;
            g.DrawLine(grid, 0, y, area.Width, y);
        }
        void Draw(IReadOnlyList<float> samples, Color color)
        {
            if (samples.Count < 2) return;
            using var pen = new Pen(color, 1.8f);
            var points = new PointF[samples.Count];
            for (int i = 0; i < samples.Count; i++)
                points[i] = new PointF(i * (area.Width - 2f) / Math.Max(1, samples.Count - 1),
                                       (area.Height - 4f) * (1f - Math.Clamp(samples[i], 0, 1)) + 2f);
            g.DrawLines(pen, points);
        }
        Draw(ramSamples, mint); Draw(cpuSamples, blue);
    }
}
