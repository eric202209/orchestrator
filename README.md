# 🦅 OpenClaw Mobile Assistant

A **privacy-first, local-first** Android chat assistant that integrates with your OpenClaw server. Built with Kotlin and Jetpack Compose.

## 🚀 Quick Overview

**What it does:**
- Clean chat interface with message history
- Local storage using Room database
- Connects to your self-hosted OpenClaw server
- **No cloud dependencies, no tracking, no data collection**

**Tech stack:**
- Kotlin + Jetpack Compose
- Room Database (local storage)
- Retrofit (API calls)
- Material Design 3

## 🚀 Quick Start

### Prerequisites
- Android Studio Hedgehog (2023.1.1) or newer
- JDK 17 or higher
- Android SDK API 24+ (Android 7.0+)

### Step 1: Create New Project
1. Open Android Studio
2. File → New → New Project
3. Select "Empty Views Activity"
4. Name: `OpenClawMobileAssistant`
5. Package name: `com.yourname.openclawassistant`
6. Language: **Kotlin**
7. Minimum SDK: **API 24** (Android 7.0)
8. Click Finish

### Step 2: Project Structure
```
OpenClawMobileAssistant/
├── app/
│   ├── src/main/
│   │   ├── java/com/yourname/openclawassistant/
│   │   │   ├── MainActivity.kt
│   │   │   ├── data/
│   │   │   │   ├── ChatMessage.kt
│   │   │   │   ├── ChatDao.kt
│   │   │   │   └── ChatDatabase.kt
│   │   │   ├── ui/
│   │   │   │   ├── ChatAdapter.kt
│   │   │   │   └── components/
│   │   │   └── service/
│   │   │       └── OpenClawService.kt
│   │   └── res/
│   └── build.gradle.kts
├── build.gradle.kts
├── settings.gradle.kts
└── .gitignore
```

### Step 3: Add Dependencies

#### Root `build.gradle.kts`:
```kotlin
plugins {
    id("com.android.application") version "8.2.0" apply false
    id("org.jetbrains.kotlin.android") version "1.9.20" apply false
    id("com.google.devtools.ksp") version "1.9.20-1.0.14" apply false
}
```

#### App `build.gradle.kts`:
```kotlin
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.devtools.ksp")
}

android {
    namespace = "com.yourname.openclawassistant"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.yourname.openclawassistant"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }
    
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    
    kotlinOptions {
        jvmTarget = "17"
    }
    
    buildFeatures {
        viewBinding = true
        compose = true
    }
    
    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.4"
    }
}

dependencies {
    // Core Android
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    
    // Jetpack Compose
    val composeBom = platform("androidx.compose:compose-bom:2023.10.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.6.2")
    implementation("androidx.activity:activity-compose:1.8.1")
    
    // Room Database (Local Storage)
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")
    
    // Retrofit (OpenClaw API)
    implementation("com.squareup.retrofit2:retrofit:2.9.0")
    implementation("com.squareup.retrofit2:converter-gson:2.9.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")
    
    // Coroutines
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
    
    // Lifecycle
    implementation("androidx.lifecycle:lifecycle-viewmodel-ktx:2.6.2")
    implementation("androidx.lifecycle:lifecycle-livedata-ktx:2.6.2")
    
    // Testing
    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.1.5")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.5.1")
    androidTestImplementation(composeBom)
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-tooling")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
}
```

### Step 4: Create Data Models

#### `data/ChatMessage.kt`:
```kotlin
package com.yourname.openclawassistant.data

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "chat_messages")
data class ChatMessage(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    
    val sessionId: String,
    val message: String,
    val isUser: Boolean,
    val timestamp: Long = System.currentTimeMillis(),
    val status: MessageStatus = MessageStatus.SENT
)

enum class MessageStatus {
    SENT, DELIVERED, READ, FAILED
}
```

#### `data/ChatDao.kt`:
```kotlin
package com.yourname.openclawassistant.data

import androidx.room.*
import kotlinx.coroutines.flow.Flow

@Dao
interface ChatDao {
    @Query("SELECT * FROM chat_messages ORDER BY timestamp DESC")
    fun getAllMessages(): Flow<List<ChatMessage>>
    
    @Query("SELECT * FROM chat_messages WHERE sessionId = :sessionId ORDER BY timestamp DESC")
    fun getMessagesBySession(sessionId: String): Flow<List<ChatMessage>>
    
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertMessage(message: ChatMessage): Long
    
    @Update
    suspend fun updateMessage(message: ChatMessage)
    
    @Delete
    suspend fun deleteMessage(message: ChatMessage)
    
    @Query("DELETE FROM chat_messages WHERE sessionId = :sessionId")
    suspend fun deleteMessagesBySession(sessionId: String)
}
```

#### `data/ChatDatabase.kt`:
```kotlin
package com.yourname.openclawassistant.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

@Database(entities = [ChatMessage::class], version = 1, exportSchema = false)
abstract class ChatDatabase : RoomDatabase() {
    abstract fun chatDao(): ChatDao
    
    companion object {
        @Volatile
        private var INSTANCE: ChatDatabase? = null
        
        fun getDatabase(context: Context): ChatDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    ChatDatabase::class.java,
                    "openclaw_database"
                )
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
```

### Step 5: Create UI Components

#### `ui/ChatAdapter.kt` (View-based adapter for backward compatibility):
```kotlin
package com.yourname.openclawassistant.ui

import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.yourname.openclawassistant.data.ChatMessage
import com.yourname.openclawassistant.databinding.ItemMessageBinding

class ChatAdapter : ListAdapter<ChatMessage, ChatAdapter.MessageViewHolder>(MessageDiffCallback()) {
    
    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): MessageViewHolder {
        val binding = ItemMessageBinding.inflate(
            LayoutInflater.from(parent.context),
            parent,
            false
        )
        return MessageViewHolder(binding)
    }
    
    override fun onBindViewHolder(holder: MessageViewHolder, position: Int) {
        holder.bind(getItem(position))
    }
    
    inner class MessageViewHolder(
        private val binding: ItemMessageBinding
    ) : RecyclerView.ViewHolder(binding.root) {
        
        fun bind(message: ChatMessage) {
            binding.apply {
                messageText.text = message.message
                isUser = message.isUser
                timestampText.text = java.text.SimpleDateFormat(
                    "HH:mm", java.util.Locale.getDefault()
                ).format(java.util.Date(message.timestamp))
                
                // Visual styling
                root.setBackgroundResource(
                    if (message.isUser) {
                        android.R.drawable.ic_dialog_info
                    } else {
                        android.R.drawable.ic_menu_compass
                    }
                )
            }
        }
    }
    
    class MessageDiffCallback : DiffUtil.ItemCallback<ChatMessage>() {
        override fun areItemsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean {
            return oldItem.id == newItem.id
        }
        
        override fun areContentsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean {
            return oldItem == newItem
        }
    }
}
```

### Step 6: OpenClaw Service Integration

#### `service/OpenClawService.kt`:
```kotlin
package com.yourname.openclawassistant.service

import com.yourname.openclawassistant.data.ChatMessage
import com.yourname.openclawassistant.data.ChatDao
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.Body
import retrofit2.http.POST

interface OpenClawApi {
    @POST("api/messages/send")
    suspend fun sendMessage(@Body request: SendMessageRequest): SendMessageResponse
    
    @POST("api/messages/receive")
    suspend fun receiveMessages(@Body request: ReceiveMessagesRequest): ReceiveMessagesResponse
}

data class SendMessageRequest(
    val sessionId: String,
    val message: String,
    val timestamp: Long
)

data class SendMessageResponse(
    val success: Boolean,
    val response: String? = null,
    val error: String? = null
)

data class ReceiveMessagesRequest(
    val sessionId: String,
    val since: Long
)

data class ReceiveMessagesResponse(
    val messages: List<ChatMessage>
)

class OpenClawService(private val chatDao: ChatDao) {
    private val retrofit = Retrofit.Builder()
        .baseUrl("http://YOUR_OPENCLAW_IP:18789/") // Replace with your server IP
        .addConverterFactory(GsonConverterFactory.create())
        .build()
    
    private val api = retrofit.create(OpenClawApi::class.java)
    
    suspend fun sendMessage(sessionId: String, message: String): SendMessageResponse {
        return withContext(Dispatchers.IO) {
            try {
                val request = SendMessageRequest(sessionId, message, System.currentTimeMillis())
                api.sendMessage(request)
            } catch (e: Exception) {
                SendMessageResponse(false, error = e.message ?: "Unknown error")
            }
        }
    }
    
    suspend fun receiveMessages(sessionId: String, since: Long): List<ChatMessage> {
        return withContext(Dispatchers.IO) {
            try {
                val request = ReceiveMessagesRequest(sessionId, since)
                api.receiveMessages(request).messages
            } catch (e: Exception) {
                emptyList()
            }
        }
    }
    
    suspend fun saveMessageToLocal(message: ChatMessage) {
        withContext(Dispatchers.IO) {
            chatDao.insertMessage(message)
        }
    }
}
```

### Step 7: Update MainActivity.kt

Replace the default `MainActivity.kt` with:

```kotlin
package com.yourname.openclawassistant

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.yourname.openclawassistant.data.ChatDatabase
import com.yourname.openclawassistant.databinding.ActivityMainBinding
import com.yourname.openclawassistant.ui.ChatAdapter
import com.yourname.openclawassistant.service.OpenClawService

class MainActivity : AppCompatActivity() {
    
    private lateinit var binding: ActivityMainBinding
    private lateinit var chatAdapter: ChatAdapter
    private lateinit var openClawService: OpenClawService
    
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        
        setupRecyclerView()
        setupOpenClawService()
        loadMessages()
    }
    
    private fun setupRecyclerView() {
        chatAdapter = ChatAdapter()
        binding.recyclerView.apply {
            layoutManager = LinearLayoutManager(this@MainActivity)
            adapter = chatAdapter
        }
    }
    
    private fun setupOpenClawService() {
        val database = ChatDatabase.getDatabase(application)
        openClawService = OpenClawService(database.chatDao())
    }
    
    private fun loadMessages() {
        // TODO: Load messages from Room database
        // This will be implemented in the next iteration
    }
    
    // TODO: Add send message functionality
    // TODO: Add message input handling
    // TODO: Implement WebSocket for real-time updates
}
```

### Step 8: Update Layout Files

#### `res/layout/activity_main.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<androidx.constraintlayout.widget.ConstraintLayout 
    xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:app="http://schemas.android.com/apk/res-auto"
    android:layout_width="match_parent"
    android:layout_height="match_parent">

    <androidx.recyclerview.widget.RecyclerView
        android:id="@+id/recyclerView"
        android:layout_width="0dp"
        android:layout_height="0dp"
        app:layout_constraintBottom_toTopOf="@id/messageInputLayout"
        app:layout_constraintEnd_toEndOf="parent"
        app:layout_constraintStart_toStartOf="parent"
        app:layout_constraintTop_toTopOf="parent" />

    <LinearLayout
        android:id="@+id/messageInputLayout"
        android:layout_width="0dp"
        android:layout_height="wrap_content"
        android:orientation="horizontal"
        android:padding="8dp"
        app:layout_constraintBottom_toBottomOf="parent"
        app:layout_constraintEnd_toEndOf="parent"
        app:layout_constraintStart_toStartOf="parent">

        <EditText
            android:id="@+id/messageEditText"
            android:layout_width="0dp"
            android:layout_height="wrap_content"
            android:layout_weight="1"
            android:hint="Type a message..."
            android:inputType="textMultiLine"
            android:maxLines="4" />

        <Button
            android:id="@+id/sendButton"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="Send" />
    </LinearLayout>

</androidx.constraintlayout.widget.ConstraintLayout>
```

#### Create `res/layout/item_message.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout 
    xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="wrap_content"
    android:orientation="vertical"
    android:padding="8dp">

    <TextView
        android:id="@+id/messageText"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:textSize="16sp"
        android:padding="8dp"
        android:background="@drawable/message_background" />

    <TextView
        android:id="@+id/timestampText"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:textSize="12sp"
        android:gravity="end"
        android:textColor="#666666"
        android:paddingTop="4dp" />

</LinearLayout>
```

#### Create `res/drawable/message_background.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<selector xmlns:android="http://schemas.android.com/apk/res/android">
    <item android:state_user="true">
        <shape android:shape="rectangle">
            <solid android:color="#E3F2FD" />
            <corners android:radius="16dp" />
        </shape>
    </item>
    <item>
        <shape android:shape="rectangle">
            <solid android:color="#FFFFFF" />
            <corners android:radius="16dp" />
        </shape>
    </item>
</selector>
```

### Step 9: Configure Permissions

#### `AndroidManifest.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    
    <!-- Optional: For voice input -->
    <uses-permission android:name="android.permission.RECORD_AUDIO" />
    
    <!-- Optional: For notifications -->
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />

    <application
        android:allowBackup="true"
        android:icon="@mipmap/ic_launcher"
        android:label="@string/app_name"
        android:roundIcon="@mipmap/ic_launcher_round"
        android:supportsRtl="true"
        android:theme="@style/Theme.OpenClawMobileAssistant"
        android:usesCleartextTraffic="true"> <!-- For local HTTP connection -->
        
        <activity
            android:name=".MainActivity"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
        
        <service
            android:name=".service.OpenClawService"
            android:enabled="true"
            android:exported="false" />
            
    </application>

</manifest>
```

### Step 10: Build and Run

1. **Connect your Android device** via USB
2. **Enable USB debugging** on your device (Settings → Developer Options)
3. **In Android Studio**: Run → Run 'app'
4. **Select your device** from the device picker
5. **Wait for build** to complete
6. **Install and test** the app on your device

---

## 🔒 Privacy & Security Features

### What's Included:
✅ **No cloud data collection** - All data stored locally  
✅ **No analytics** - No tracking libraries  
✅ **No third-party APIs** - Only connects to your OpenClaw server  
✅ **Local storage only** - Room database on device  
✅ **Open source** - Full transparency  

### What's NOT Included:
❌ No Google Analytics  
❌ No Firebase  
❌ No Crashlytics  
❌ No user accounts  
❌ No cloud sync (yet)  

---

## 📦 Distribution Options

### Option 1: GitHub Releases (Recommended)
```bash
# Build release APK
./gradlew assembleRelease

# APK will be at: app/build/outputs/apk/release/app-release.apk
# Upload to GitHub Releases manually
```

### Option 2: F-Droid
- Create a F-Droid repo
- Submit for review
- Great for privacy-focused apps

### Option 3: Google Play Store
- Create Google Play Developer account ($25 one-time)
- Submit APK for review
- Broader audience

---

## 🚧 Next Steps (To Be Implemented)

1. **Implement message input handling** in `MainActivity.kt`
2. **Add WebSocket connection** for real-time updates
3. **Implement voice input** using Android Speech-to-Text
4. **Add encryption** for local storage (optional)
5. **Create settings screen** for OpenClaw server configuration
6. **Add notification support** for new messages
7. **Implement session management** for multiple conversations
8. **Add export/import** functionality for chat history

---

## 🛠️ Troubleshooting

### Common Issues:

**1. "Cannot resolve symbol" errors**
- Run: `File → Invalidate Caches / Restart`
- Sync Gradle files

**2. Build fails with KSP errors**
- Ensure KSP plugin version matches Kotlin version
- Clean project: `Build → Clean Project`

**3. App won't install on device**
- Check USB debugging is enabled
- Try different USB cable
- Restart ADB: `adb kill-server` then `adb start-server`

**4. HTTP connection fails**
- Ensure `usesCleartextTraffic="true"` in manifest
- Check OpenClaw server is running on correct IP
- Use same WiFi network on phone and computer

---

## 📝 Git Setup

### Initialize Git Repository:
```bash
cd OpenClawMobileAssistant
git init
git add .
git commit -m "Initial commit: OpenClaw Mobile Assistant"
```

### Push to GitHub (Optional):
```bash
git remote add origin https://github.com/YOUR_USERNAME/OpenClawMobileAssistant.git
git branch -M main
git push -u origin main
```

**Note:** This project is designed for local-only development. You're not required to push to GitHub.

---

## 🎯 Success Criteria

- ✅ App builds successfully
- ✅ Installs on your Android device
- ✅ Can view chat messages (local storage)
- ✅ Can send messages to OpenClaw server
- ✅ No privacy violations
- ✅ Works offline (local features)

---

## 📚 Resources

- [Android Development Documentation](https://developer.android.com/)
- [Jetpack Compose Guide](https://developer.android.com/jetpack/compose)
- [Room Database Guide](https://developer.android.com/training/data-storage/room)
- [Retrofit Guide](https://square.github.io/retrofit/)
- [OpenClaw Documentation](https://docs.openclaw.ai/)

---

*Built with privacy-first principles. No data leaves your device.* 🦅
