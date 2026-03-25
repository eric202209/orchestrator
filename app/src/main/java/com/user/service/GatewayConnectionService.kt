package com.user.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.user.ui.activities.MainActivity
import com.user.data.PrefsManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

class GatewayConnectionService : Service() {

    companion object {
        const val CHANNEL_ID      = "openclaw_gateway"
        const val NOTIFICATION_ID = 1001
        const val ACTION_STOP     = "com.user.STOP_SERVICE"
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private lateinit var gatewayClient: GatewayClient
    private lateinit var ed25519: Ed25519Manager

    override fun onCreate() {
        super.onCreate()
        val prefs   = PrefsManager(this)
        ed25519     = Ed25519Manager(this)
        gatewayClient = GatewayClient(prefs.serverUrl, prefs.gatewayToken, ed25519)
        createNotificationChannel()

        // Persist the device ID after successful pairing
        if (ed25519.isPaired()) {
            // Device already paired, ensure we have the stored ID
            prefs.deviceId = ed25519.deviceId
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Handle mark_paired action
        if (intent?.action == "mark_paired") {
            val deviceId = intent.getStringExtra("deviceId")
            if (!deviceId.isNullOrEmpty()) {
                markPaired()
                // Update notification to reflect connected state
                updateNotification("● Connected", "Main")
            }
            return START_NOT_STICKY
        }

        val status = intent?.getStringExtra("status")
        val agent  = intent?.getStringExtra("agent")
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                buildNotification("○ Connecting…", ""),
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            )
        } else {
            startForeground(NOTIFICATION_ID, buildNotification("○ Connecting…", ""))
        }
        if (status != null) {
            updateNotification(status, agent ?: "")
            return START_STICKY
        }

        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        super.onDestroy()
        scope.cancel()
        gatewayClient.disconnect()
    }

    // Called when pairing is successful, persists the device ID
    fun markPaired() {
        ed25519.persistDeviceId()
        PrefsManager(this).deviceId = ed25519.deviceId
    }

    // ── Notification ─────────────────────────────────────────

    private fun buildNotification(status: String, agent: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val stopIntent = PendingIntent.getService(
            this, 0,
            Intent(this, GatewayConnectionService::class.java).apply {
                action = ACTION_STOP
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val contentText = if (agent.isNotEmpty())
            "$status · Agent: $agent"
        else
            status

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("OpenClaw Mobile")
            .setContentText(contentText)
            .setContentIntent(openIntent)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop", stopIntent)
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
    }

    private fun updateNotification(status: String, agent: String) {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(NOTIFICATION_ID, buildNotification(status, agent))
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "OpenClaw Gateway",
                NotificationManager.IMPORTANCE_MIN
            ).apply {
                description = "Keeps connection to OpenClaw Gateway active"
                setShowBadge(false)
            }
            val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(channel)
        }
    }
}

