# OpenClaw Mobile Assistant

A privacy-first Android chat app that connects to your self-hosted OpenClaw server.

## Tech Stack

- Kotlin + Views (XML layouts)
- Room Database (local message storage)
- Retrofit (HTTP API)
- Material Design 3

## Project Structure

```
app/src/main/java/com/user/
├── MainActivity.kt
├── data/
│   ├── ChatMessage.kt
│   ├── ChatDao.kt
│   └── ChatDatabase.kt
├── ui/
│   └── ChatAdapter.kt
└── service/
    └── OpenClawService.kt
```

## Quick Start

### Prerequisites
- Android Studio Hedgehog (2023.1.1) or newer
- JDK 17+
- Android SDK API 24+ (Android 7.0+)

### Setup

1. Clone or create a new project with package `com.user`, minimum SDK 24
2. Copy all files from `code.txt` into the matching paths
3. In `OpenClawService.kt`, replace `YOUR_OPENCLAW_IP` with your server's local IP
4. Sync Gradle, then build and run

### Build & Run

```bash
./gradlew assembleDebug
# APK → app/build/outputs/apk/debug/app-debug.apk
```

Or use **Run → Run 'app'** in Android Studio with a connected device.

## Troubleshooting

| Issue | Fix |
|---|---|
| "Cannot resolve symbol" | File → Invalidate Caches / Restart |
| KSP build errors | Ensure KSP version matches Kotlin version |
| HTTP connection fails | Check `usesCleartextTraffic="true"` in manifest; confirm device and server are on the same network |
| App won't install | Enable USB debugging; try `adb kill-server && adb start-server` |

## Privacy

- No cloud data collection — all messages stored locally via Room
- No analytics, no Firebase, no third-party tracking
- Only outbound connection is to your own OpenClaw server
