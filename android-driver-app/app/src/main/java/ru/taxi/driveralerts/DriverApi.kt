package ru.taxi.driveralerts

import android.content.Context
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Credentials never appear in URLs, logs or saved command payloads. */
class DriverApi(context: Context) {
    private val prefs = AppPrefs.of(context)
    private val client = OkHttpClient.Builder().connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS).callTimeout(15, TimeUnit.SECONDS).build()
    fun post(path: String, body: JSONObject = JSONObject(), done: (Int, JSONObject?) -> Unit): Call {
        body.put("device_id", prefs.getLong("device_id", 0L))
            .put("device_secret", prefs.getString("device_secret", ""))
        val request = Request.Builder().url(BuildConfig.SERVER_URL.trimEnd('/') + path)
            .post(body.toString().toRequestBody("application/json".toMediaType())).build()
        return client.newCall(request).also { call -> call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) { if (!call.isCanceled()) done(0, null) }
            override fun onResponse(call: Call, response: Response) {
                response.use { done(it.code, runCatching { JSONObject(it.body?.string().orEmpty()) }.getOrNull()) }
            }
        }) }
    }
    fun cancel() { client.dispatcher.cancelAll() }
}
