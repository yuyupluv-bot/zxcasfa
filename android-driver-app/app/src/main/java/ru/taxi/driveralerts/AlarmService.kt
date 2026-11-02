package ru.taxi.driveralerts

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.provider.Settings
import androidx.core.app.NotificationCompat

class AlarmService : Service() {
    companion object {
        const val ACTION_STATE = "ru.taxi.driveralerts.ALARM_STATE"
        private const val CHANNEL_ID = "driver_alerts_v3"
        private const val NOTIFICATION_ID = 77
    }

    private var player: MediaPlayer? = null
    private var vibrator: Vibrator? = null
    private val handler = Handler(Looper.getMainLooper())
    private var repeatMilliseconds = 5_000L
    private var shouldVibrate = true
    private var volume = 1f
    private var active = false
    private var fallbackAttempted = false

    override fun onBind(intent: Intent?) = null

    override fun onStartCommand(intent: Intent, flags: Int, startId: Int): Int {
        if (intent.action == "STOP") {
            stopSelf()
            return START_NOT_STICKY
        }
        val prefs = AppPrefs.of(this)
        val event = intent.getStringExtra("event")
        if (!prefs.getBoolean(AppPrefs.NOTIFICATIONS, true) ||
            !AppPrefs.isScenarioEnabled(prefs, event)
        ) {
            stopSelf()
            return START_NOT_STICKY
        }

        active = true
        fallbackAttempted = false
        repeatMilliseconds = (intent.getIntExtra("repeat", 5).coerceIn(1, 60) * 1_000).toLong()
        shouldVibrate = prefs.getBoolean(AppPrefs.VIBRATION, true) &&
            intent.getStringExtra("vibrate") != "0"
        volume = prefs.getInt(AppPrefs.VOLUME, 100).coerceIn(0, 100) / 100f

        createNotificationChannel()
        val open = PendingIntent.getActivity(
            this,
            1,
            Intent(this, DriverActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this,
            2,
            Intent(this, AlarmService::class.java).setAction("STOP"),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        startForeground(
            NOTIFICATION_ID,
            NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_beztaksy)
                .setColor(getColor(R.color.notification_accent))
                .setContentTitle(intent.getStringExtra("title") ?: "Новая заявка")
                .setContentText(intent.getStringExtra("route") ?: "Откройте приложение")
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setContentIntent(open)
                .addAction(0, "Открыть заявку", open)
                .addAction(0, "Остановить звук", stop)
                .setOngoing(true)
                .build(),
        )

        vibrator = if (Build.VERSION.SDK_INT >= 31) {
            getSystemService(VibratorManager::class.java).defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            getSystemService(VIBRATOR_SERVICE) as Vibrator
        }

        preparePlayer(intent.getStringExtra("sound_url"))
        sendBroadcast(Intent(ACTION_STATE).setPackage(packageName).putExtra("playing", true))
        handler.removeCallbacksAndMessages(null)
        handler.postDelayed(
            { stopSelf() },
            (intent.getIntExtra("duration", 60).coerceIn(1, 600) * 1_000).toLong(),
        )
        return START_NOT_STICKY
    }

    private fun createNotificationChannel() {
        val manager = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Заявки водителю",
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = "Заявки, параллельные заявки и отмены"
            enableVibration(false) // Вибрацией управляет пользователь внутри приложения.
            setSound(null, null) // Звук воспроизводится сервисом с выбранной громкостью.
            lockscreenVisibility = NotificationCompat.VISIBILITY_PUBLIC
        }
        manager.createNotificationChannel(channel)
    }

    private fun preparePlayer(soundPath: String?) {
        player?.release()
        player = MediaPlayer().apply {
            setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build()
            )
            setWakeMode(this@AlarmService, PowerManager.PARTIAL_WAKE_LOCK)
            if (!soundPath.isNullOrBlank()) {
                setDataSource(BuildConfig.SERVER_URL.trimEnd('/') + soundPath)
            } else {
                setDataSource(this@AlarmService, Settings.System.DEFAULT_ALARM_ALERT_URI)
            }
            isLooping = false
            setVolume(volume, volume)
            setOnPreparedListener { startCycle() }
            setOnCompletionListener { handler.postDelayed({ startCycle() }, repeatMilliseconds) }
            setOnErrorListener { _, _, _ ->
                if (!fallbackAttempted) {
                    fallbackAttempted = true
                    preparePlayer(null)
                } else {
                    stopSelf()
                }
                true
            }
            prepareAsync()
        }
    }

    private fun startCycle() {
        val current = player ?: return
        if (!active) return
        try {
            current.seekTo(0)
            current.setVolume(volume, volume)
            current.start()
            if (shouldVibrate) {
                vibrator?.vibrate(
                    VibrationEffect.createWaveform(longArrayOf(0, 650, 250), -1)
                )
            }
        } catch (_: IllegalStateException) {
            if (!fallbackAttempted) {
                fallbackAttempted = true
                preparePlayer(null)
            } else {
                stopSelf()
            }
        }
    }

    override fun onDestroy() {
        active = false
        handler.removeCallbacksAndMessages(null)
        try {
            player?.stop()
        } catch (_: Exception) {
            // MediaPlayer may still be preparing.
        }
        player?.release()
        player = null
        vibrator?.cancel()
        sendBroadcast(Intent(ACTION_STATE).setPackage(packageName).putExtra("playing", false))
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }
}
