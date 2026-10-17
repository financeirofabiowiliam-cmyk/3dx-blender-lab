# 3DX Hybrid Desktop (WinForms + Edge WebView2)
- Source of truth: CloudyHellboy/ANGELS-NIGHT-WORLD.
- One native desktop window with two tabs: embedded official 3DX Studio WebGL via WebView2 and local Windows CPU/RAM processor connected over the unchanged authenticated Render endpoints.
- Android + Windows selection is handled by the **official Studio** through preprocess_device='both'; the desktop companion only leases Windows stages and never changes the game.
- Lab WebView2 renders WebGL inside the native program. This still uses WebView2 subprocess RAM; it is **not** a RAM-free web renderer. Use the Free memory button to unload its renderer and keep the lab job progressing on Render.
- Worker subprocess uses Python 3.11+ and NumPy + Pillow, secrets are sent through redirected stdin. Server and mobile still use the existing token and endpoint contracts.
- RAM protection presets: balanced (start 950 MB free, stop below 650 MB); intensive (start 650 MB, stop below 400 MB). We do **not** disable OS memory/temperature protections.
- Build: dotnet publish -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:PublishReadyToRun=false
- Windows needs Microsoft Edge WebView2 Runtime for the 3D lab. https://developer.microsoft.com/microsoft-edge/webview2/
- GitHub secondary account acts as build infrastructure only; this folder is the canonical app source, not a new lab.
