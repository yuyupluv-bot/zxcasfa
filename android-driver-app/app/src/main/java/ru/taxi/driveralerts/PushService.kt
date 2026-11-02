package ru.taxi.driveralerts

import android.content.Intent
import androidx.core.content.ContextCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException

class PushService : FirebaseMessagingService() {
    companion object {
        private val client = OkHttpClient()
    }

    override fun onNewToken(token: String) {
        val prefs = AppPrefs.of(this)
        val deviceId = prefs.getLong("device_id", 0L)
        val deviceSecret = prefs.getString("device_secret", "").orEmpty()
        if (!prefs.getBoolean(AppPrefs.PAIRED, false) || deviceId == 0L ||
            deviceSecret.isBlank() || BuildConfig.SERVER_URL.contains("CHANGE-ME")
        ) {
            return
        }
        val json = JSONObject()
            .put("device_id", deviceId)
            .put("device_secret", deviceSecret)
            .put("token", token)
        val request = Request.Builder()
            .url(BuildConfig.SERVER_URL.trimEnd('/') + "/api/driver-app/token")
            .post(json.toString().toRequestBody("application/json".toMediaType()))
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) = Unit
            override fun onResponse(call: Call, response: Response) = response.close()
        })
    }

    override fun onMessageReceived(message: RemoteMessage) {
        sendBroadcast(Intent("ru.taxi.driveralerts.REFRESH").setPackage(packageName))
        val prefs = AppPrefs.of(this)
        val event = message.data["event"]
        if (!prefs.getBoolean(AppPrefs.PAIRED, false) ||
            !prefs.getBoolean(AppPrefs.NOTIFICATIONS, true) ||
            !AppPrefs.isScenarioEnabled(prefs, event)
        ) {
            return
        }
        val data = message.data
        val alarm = Intent(this, AlarmService::class.java).apply {
            data.forEach { putExtra(it.key, it.value) }
            putExtra("duration", data["duration"]?.toIntOrNull() ?: 60)
            putExtra("repeat", data["repeat"]?.toIntOrNull() ?: 5)
        }
        runCatching { ContextCompat.startForegroundService(this, alarm) }
    }
}
