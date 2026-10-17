# 3DX Hybrid Mobile — aplicativo nativo para Galaxy A11

Faz processamento leve de imagens sem depender da aba do navegador. Integra-se
ao **mesmo** laboratório https://threedx-studio-blender-qa.onrender.com/studio.

## Como compilar

Abra `tools/3dx/android-hybrid-lite` no Android Studio com JDK 17 e Android
SDK 35. Execute `gradle :app:assembleDebug` (Gradle 8.10.2 ou mais recente).
O arquivo gerado estará em
`app/build/outputs/apk/debug/app-debug.apk`.

Opcionalmente execute manualmente o workflow
`Build 3DX Hybrid Android APK` na aba Actions do projeto oficial.
O pacote de teste fica nos artefatos desse workflow; ele não altera o Render.

## Como conectar

1. Instale o APK no Galaxy A11.
2. Abra o aplicativo, insira a chave do mesmo laboratório e toque em
   **Conectar e manter auxiliar ativo**.
3. Mantenha a notificação do 3DX ativa e, nas configurações Samsung do
   aplicativo, selecione bateria sem restrições quando disponível.
4. No computador, entre no 3DX Studio oficial e marque **Enviar preparação
   das fotos ao Android** nas configurações avançadas.
5. Gere um avatar com fotos; o telefone prepara imagens e o Render continua
   com reconstrução, Blender e GLB.

**Observação:** não é possível garantir conectividade contínua 24 horas por dia.
Mesmo um serviço Android em primeiro plano pode ser suspenso pelo fabricante,
bateria, falta de rede ou restrições de versão. O servidor devolve tarefas
abandonadas automaticamente à fila Render. Android 15 com targetSdk 35+
tem limite de 6 horas em 24 horas para FGS dataSync.

O aplicativo usa somente os endpoints já existentes:
`/api/v1/worker/mobile/claim`, `/api/v1/worker/mobile/complete` e
`/api/v1/worker/abandon`. Não abre portas no aparelho, nem grava a chave em
texto simples: ela é protegida pelo Android Keystore AES-GCM. A conexão é HTTPS,
Wi-Fi somente por padrão, limitada a uma tarefa de cada vez, com no máximo
768px por imagem devolvida. Em RAM disponível abaixo de 400 MiB ou bateria
abaixo de 20%, o aplicativo interrompe a execução da tarefa.

**Limite de responsabilidade:** o dispositivo não acrescenta RAM física ao
Render e não resolve sozinho falhas de memória na geração Blender de malhas altas.

Para parar, use o botão Desconectar ou a ação Parar na notificação.
