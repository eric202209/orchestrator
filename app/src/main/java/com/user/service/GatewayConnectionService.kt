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
import com.user.MainActivity
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

    override fun onCreate() {
        super.onCreate()
        val prefs   = PrefsManager(this)
        val ed25519 = Ed25519Manager(this)
        gatewayClient = GatewayClient(prefs.serverUrl, prefs.gatewayToken, ed25519)
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
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
        gatewayClient.connect()

        // Update notification on connection state changes
        scope.launch {
            gatewayClient.events.collect { event ->
                when (event) {
                    is GatewayEvent.Ready -> {
                        val agent = gatewayClient.availableAgents.firstOrNull()?.name ?: "Main"
                        updateNotification("● Connected", agent)
                    }
                    is GatewayEvent.Disconnected ->
                        updateNotification("○ Disconnected", "")
                    is GatewayEvent.Error ->
                        updateNotification("✕ Error", "")
                    else -> {}
                }
            }
        }

        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        super.onDestroy()
        scope.cancel()
        gatewayClient.disconnect()
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

