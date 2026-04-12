# 🦞 ClawMobile

A native Android client for [OpenClaw](https://github.com/openclaw/openclaw) — chat with your self-hosted AI agent from your phone.

[![Release](https://img.shields.io/github/v/release/henrycode03/clawmobile?style=flat-square&label=release&color=555555)](https://github.com/henrycode03/clawmobile/releases)
[![Downloads](https://img.shields.io/github/downloads/henrycode03/clawmobile/total?style=flat-square&label=downloads&color=4c9be8)](https://github.com/henrycode03/clawmobile/releases)
[![License](https://img.shields.io/github/license/henrycode03/clawmobile?style=flat-square&color=blue)](https://github.com/henrycode03/clawmobile/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-Android%207.0+-00A884?style=flat-square)](https://developer.android.com/about/versions/nougat)

---

## Features

- **Real-time streaming** — token-by-token typewriter response via WebSocket
- **Ed25519 authentication** — full device pairing handshake with OpenClaw Gateway 2.13+
- **Agent selector** — switch between Main / Engineer / QA agents on the fly
- **Markdown rendering** — bold, italic, inline code, code blocks, headings, lists
- **Chat history** — local Room database, sortable, per-session
- **Auto-reconnect** — recovers from network drops automatically
- **Voice input** — Android Speech-to-Text integration
- **Dark theme** — WhatsApp-style dark UI

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Kotlin |
| UI | XML Views + ViewBinding |
| Architecture | MVVM (ViewModel + LiveData + Repository) |
| Local storage | Room Database |
| Networking | OkHttp WebSocket + Retrofit |
| Auth | Ed25519 via BouncyCastle |
| Build | Gradle KTS + KSP |

## Project Structure

```
app/src/main/java/com/user/
├── data/               # Room entities, DAO, database, prefs
├── repository/         # ChatRepository — single source of truth
├── viewmodel/          # ChatViewModel, SessionViewModel
├── service/            # GatewayClient, Ed25519Manager, OpenClawService
├── ui/                 # ChatAdapter, SessionAdapter, MarkdownRenderer
├── MainActivity.kt
├── SessionsActivity.kt
└── SettingsActivity.kt
```

## Quick Start

### Recommended Architecture

Use `clawmobile` as the mobile control/chat client for the OpenClaw Gateway, and keep Orchestrator as a separate control plane:

```text
ClawMobile -> Tailscale / LAN -> GX10 Host -> OpenClaw Gateway (:8000)
                                          -> OpenClaw agent runtime
Orchestrator Dashboard/API (:8080) ------> manages sessions, approvals, logs
```

This is the best fit for the current app because `clawmobile` already speaks the OpenClaw Gateway protocol directly. It does not yet speak the Orchestrator auth/session API, so pointing the app at `:8080` will not work as a drop-in replacement.

### Prerequisites

- Android Studio Hedgehog (2023.1.1) or newer
- JDK 17+
- Android SDK API 24+ (Android 7.0+)
- OpenClaw running on your local machine or server

### Connect ClawMobile to GX10 OpenClaw Gateway

**Recommended — Tailscale to GX10**
```text
Server URL: http://<gx10-tailscale-ip>:8000
```

**Option A — SSH tunnel (emulator or phone on different network)**
```powershell
# Run on your laptop, keep it open
ssh -N -L 18789:localhost:18789 -o ServerAliveInterval=30 USER@YOUR_SERVER_IP
```
Then set Server URL in the app to `http://your_host_ip:18789` (emulator) or `http://localhost:18789` (phone on same network).

**Option B — Same WiFi**

Set Server URL to your GX10 host IP, e.g. `http://192.xxx.x.xx:8000`.

**Important**

- `clawmobile` should point to the OpenClaw Gateway URL, typically port `8000` on Asus GX10 host.
- Orchestrator stays separate on port `8080` for dashboard/API use.
- If you want mobile approval controls for Orchestrator, add that as a dedicated API integration instead of replacing the Gateway connection.

### Get Your Gateway Token

Run on your OpenClaw server:
```bash
cat ~/.openclaw/openclaw.json
# Copy the value of gateway.auth.token
```

### Build & Run

```bash
./gradlew assembleDebug
# APK → app/build/outputs/apk/debug/app-debug.apk
```

Or use **Run → Run 'app'** in Android Studio with a connected device.

### First Launch

1. App opens Settings automatically if no token is set
2. Enter Server URL and Gateway Token → Save
3. First connection shows **Device Pairing Required** dialog
4. Run on your server:
```bash
openclaw gateway call device.pair.list --json
openclaw gateway call device.pair.approve --params '{"requestId":"<id>"}' --json
```
5. Restart the app → `● Connected`

## Troubleshooting

| Issue | Fix |
|---|---|
| `● Handshaking…` stuck | Check SSH tunnel is running; verify token |
| Pairing required on every launch | Use a real device instead of emulator (emulator clears data on restart) |
| `Software caused connection abort` | Add `-o ServerAliveInterval=30` to your SSH tunnel command |
| KSP build errors | Ensure KSP version prefix matches Kotlin version in `libs.versions.toml` |
| `Cannot resolve symbol` | File → Invalidate Caches / Restart |

## Related Projects

| Project                                                    | Description                                       |
|------------------------------------------------------------|---------------------------------------------------|
| [OpenClaw](https://github.com/openclaw/openclaw)           | AI Agent framework                                |
| [orchestrator](https://github.com/henrycode03/orchestrator) | Automating software projects with OpenClaw agents |

## Privacy

- All messages stored locally on device via Room
- No analytics, no cloud sync, no third-party SDKs
- Only outbound connection is to your own OpenClaw server

## Star History

Consider giving it a star — it helps others discover the project and keeps us motivated!

<p align="center">
  <a href="https://github.com/eric202209/clawmobile/stargazers">
    <img src="https://img.shields.io/github/stars/eric202209/clawmobile?style=for-the-badge&logo=github&color=yellow" alt="GitHub Stars" />
  </a>
</p>

<p align="center">
  <a href="https://star-history.com/#eric202209/clawmobile&Date">
    <img src="https://api.star-history.com/svg?repos=eric202209/clawmobile&type=Date" width="600" alt="Star History Chart" />
  </a>
</p>

## License

MIT

