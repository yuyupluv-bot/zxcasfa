package ru.taxi.driveralerts

import android.Manifest
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.RadioButton
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.core.view.GravityCompat
import androidx.drawerlayout.widget.DrawerLayout
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.navigation.NavigationView
import com.google.firebase.messaging.FirebaseMessaging
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException

class MainActivity : AppCompatActivity() {
    private val client by lazy { OkHttpClient() }
    private lateinit var drawer: DrawerLayout
    private lateinit var toolbar: MaterialToolbar
    private lateinit var mainScreen: View
    private lateinit var settingsScreen: View
    private lateinit var testButton: Button
    private lateinit var toggleNotifications: Button
    private lateinit var connectionText: TextView
    private lateinit var statusDot: TextView
    private lateinit var notificationStatus: TextView
    private lateinit var backgroundStatus: TextView
    private var testing = false
    private var receiverRegistered = false
    private var openBatteryAfterPermission = false

    private val notificationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) {
        refreshReadiness()
        if (openBatteryAfterPermission) {
            openBatteryAfterPermission = false
            openBatterySettings()
        }
    }

    private val alarmReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            testing = intent?.getBooleanExtra("playing", false) == true
            renderTestButton()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        val initialPrefs = AppPrefs.of(this)
        AppCompatDelegate.setDefaultNightMode(
            if (initialPrefs.getString(AppPrefs.THEME, "dark") == "light") {
                AppCompatDelegate.MODE_NIGHT_NO
            } else {
                AppCompatDelegate.MODE_NIGHT_YES
            }
        )
        super.onCreate(savedInstanceState)
        if (initialPrefs.getBoolean(AppPrefs.PAIRED, false) && !intent.getBooleanExtra("settings_only",false)) {
            startActivity(Intent(this,DriverActivity::class.java)); finish(); return
        }
        setContentView(R.layout.activity_main)

        registerAlarmReceiver()
        bindNavigation()
        bindMainScreen()
        bindSettings()
        if (intent.getBooleanExtra("settings_only",false)) showScreen(main=false)
        renderTestButton()
        renderNotificationState()
        refreshReadiness()

        if (!initialPrefs.getBoolean(AppPrefs.ONBOARDING_SHOWN, false)) {
            initialPrefs.edit().putBoolean(AppPrefs.ONBOARDING_SHOWN, true).apply()
            findViewById<View>(R.id.readinessCard).post { showBackgroundOnboarding() }
        }
    }

    private fun registerAlarmReceiver() {
        val filter = IntentFilter(AlarmService.ACTION_STATE)
        ContextCompat.registerReceiver(
            this,
            alarmReceiver,
            filter,
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        receiverRegistered = true
    }

    private fun bindNavigation() {
        drawer = findViewById(R.id.drawerLayout)
        toolbar = findViewById(R.id.toolbar)
        mainScreen = findViewById(R.id.mainScreen)
        settingsScreen = findViewById(R.id.settingsScreen)
        val navigation = findViewById<NavigationView>(R.id.navigationView)
        toolbar.setNavigationOnClickListener { drawer.openDrawer(GravityCompat.START) }
        navigation.setCheckedItem(R.id.navMain)
        navigation.setNavigationItemSelectedListener { item ->
            when (item.itemId) {
                R.id.navMain -> if (AppPrefs.of(this).getBoolean(AppPrefs.PAIRED,false)) { startActivity(Intent(this,DriverActivity::class.java)); finish() } else showScreen(main=true)
                R.id.navSettings -> showScreen(main = false)
                else -> return@setNavigationItemSelectedListener false
            }
            drawer.closeDrawer(GravityCompat.START)
            true
        }
    }

    private fun showScreen(main: Boolean) {
        val visible = if (main) mainScreen else settingsScreen
        val hidden = if (main) settingsScreen else mainScreen
        hidden.visibility = View.GONE
        visible.visibility = View.VISIBLE
        toolbar.title = if (main) "Главная" else "Настройки"
        visible.alpha = 0f
        visible.translationY = 12f
        visible.animate().alpha(1f).translationY(0f).setDuration(220L).start()
        if (main) refreshReadiness()
    }

    private fun bindMainScreen() {
        val prefs = AppPrefs.of(this)
        val codeSection = findViewById<View>(R.id.codeSection)
        val dashboard = findViewById<View>(R.id.dashboard)
        val code = findViewById<EditText>(R.id.code)
        val status = findViewById<TextView>(R.id.status)
        val driverName = findViewById<TextView>(R.id.driverName)
        testButton = findViewById(R.id.test)
        toggleNotifications = findViewById(R.id.toggleNotifications)
        connectionText = findViewById(R.id.connectionText)
        statusDot = findViewById(R.id.statusDot)
        notificationStatus = findViewById(R.id.notificationStatus)
        backgroundStatus = findViewById(R.id.backgroundStatus)

        fun showDashboard(name: String) {
            codeSection.visibility = View.GONE
            dashboard.visibility = View.VISIBLE
            driverName.text = name
            renderNotificationState()
        }

        if (prefs.getBoolean(AppPrefs.PAIRED, false)) {
            showDashboard(prefs.getString(AppPrefs.DRIVER, "Водитель") ?: "Водитель")
        }

        findViewById<Button>(R.id.connect).setOnClickListener {
            val value = code.text.toString().trim()
            if (value.length !in 6..10 || !value.all(Char::isDigit)) {
                status.text = "Введите код из 6–10 цифр"
                return@setOnClickListener
            }
            if (BuildConfig.SERVER_URL.contains("CHANGE-ME")) {
                status.text = "В APK не настроен адрес сервера"
                return@setOnClickListener
            }
            status.text = "Подключение…"
            FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
                val json = JSONObject().put("code", value).put("token", token)
                val request = Request.Builder()
                    .url(BuildConfig.SERVER_URL.trimEnd('/') + "/api/driver-app/register")
                    .post(json.toString().toRequestBody("application/json".toMediaType()))
                    .build()
                client.newCall(request).enqueue(object : Callback {
                    override fun onFailure(call: Call, e: IOException) = runOnUiThread {
                        status.text = "Ошибка сети. Проверьте подключение"
                    }

                    override fun onResponse(call: Call, response: Response) {
                        response.use {
                            val body = it.body?.string().orEmpty()
                            runOnUiThread {
                                if (it.isSuccessful) {
                                    val result = JSONObject(body)
                                    val name = result.optString("driver", "Водитель")
                                    prefs.edit()
                                        .putBoolean(AppPrefs.PAIRED, true)
                                        .putBoolean(AppPrefs.NOTIFICATIONS, true)
                                        .putString(AppPrefs.DRIVER, name)
                                        .putLong("device_id", result.optLong("device_id", 0L))
                                        .putString("device_secret", result.optString("device_secret", ""))
                                        .apply()
                                    code.setText("")
                                    showDashboard(name)
                                    refreshReadiness()
                                    startActivity(Intent(this@MainActivity,DriverActivity::class.java))
                                    finish()
                                } else {
                                    status.text = if (it.code == 403) {
                                        "Код неверный или истёк"
                                    } else {
                                        "Ошибка подключения: ${it.code}"
                                    }
                                }
                            }
                        }
                    }
                })
            }.addOnFailureListener {
                status.text = "Не удалось получить токен уведомлений"
            }
        }

        testButton.setOnClickListener {
            if (testing) {
                stopService(Intent(this, AlarmService::class.java).setAction("STOP"))
            } else {
                if (!prefs.getBoolean(AppPrefs.NOTIFICATIONS, true)) {
                    Toast.makeText(this, "Сначала включите уведомления", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }
                ContextCompat.startForegroundService(
                    this,
                    Intent(this, AlarmService::class.java)
                        .putExtra("event", "test")
                        .putExtra("title", "Тест звука")
                        .putExtra("route", "Проверка громкости и вибрации")
                        .putExtra("duration", 20)
                        .putExtra("repeat", 5)
                        .putExtra("vibrate", "1"),
                )
            }
        }

        toggleNotifications.setOnClickListener {
            val enabled = !prefs.getBoolean(AppPrefs.NOTIFICATIONS, true)
            prefs.edit().putBoolean(AppPrefs.NOTIFICATIONS, enabled).apply()
            if (enabled) requestNotificationPermission(openBatteryAfter = false)
            if (!enabled) stopService(Intent(this, AlarmService::class.java).setAction("STOP"))
            renderNotificationState()
            refreshReadiness()
        }
        findViewById<Button>(R.id.backgroundButton).setOnClickListener {
            requestNotificationPermission(openBatteryAfter = true)
        }
    }

    private fun bindSettings() {
        val prefs = AppPrefs.of(this)
        val volumeValue = findViewById<TextView>(R.id.volumeValue)
        val volume = findViewById<SeekBar>(R.id.volumeSeek)
        val vibration = findViewById<MaterialSwitch>(R.id.vibrationSwitch)
        val order = findViewById<MaterialSwitch>(R.id.orderSwitch)
        val parallel = findViewById<MaterialSwitch>(R.id.parallelSwitch)
        val cancelled = findViewById<MaterialSwitch>(R.id.cancelSwitch)
        val light = findViewById<RadioButton>(R.id.themeLight)
        val dark = findViewById<RadioButton>(R.id.themeDark)

        val savedVolume = prefs.getInt(AppPrefs.VOLUME, 100).coerceIn(0, 100)
        volume.progress = savedVolume
        volumeValue.text = "Громкость сигнала · $savedVolume%"
        vibration.isChecked = prefs.getBoolean(AppPrefs.VIBRATION, true)
        order.isChecked = prefs.getBoolean(AppPrefs.SCENARIO_ORDER, true)
        parallel.isChecked = prefs.getBoolean(AppPrefs.SCENARIO_PARALLEL, true)
        cancelled.isChecked = prefs.getBoolean(AppPrefs.SCENARIO_CANCELLED, true)
        val isLight = prefs.getString(AppPrefs.THEME, "dark") == "light"
        light.isChecked = isLight
        dark.isChecked = !isLight

        volume.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar?, progress: Int, fromUser: Boolean) {
                volumeValue.text = "Громкость сигнала · $progress%"
                if (fromUser) prefs.edit().putInt(AppPrefs.VOLUME, progress).apply()
            }
            override fun onStartTrackingTouch(seekBar: SeekBar?) = Unit
            override fun onStopTrackingTouch(seekBar: SeekBar?) = Unit
        })
        vibration.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean(AppPrefs.VIBRATION, checked).apply()
        }
        order.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean(AppPrefs.SCENARIO_ORDER, checked).apply()
        }
        parallel.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean(AppPrefs.SCENARIO_PARALLEL, checked).apply()
        }
        cancelled.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean(AppPrefs.SCENARIO_CANCELLED, checked).apply()
        }
        findViewById<View>(R.id.themeGroup).setOnClickListener { }
        light.setOnClickListener { applyTheme("light") }
        dark.setOnClickListener { applyTheme("dark") }

        findViewById<Button>(R.id.notificationSettingsButton).setOnClickListener {
            openNotificationSettings()
        }
        findViewById<Button>(R.id.batterySettingsButton).setOnClickListener {
            openBatterySettings()
        }
        findViewById<Button>(R.id.privacyButton).setOnClickListener {
            if (BuildConfig.PRIVACY_POLICY_URL.contains("CHANGE-ME")) {
                Toast.makeText(this, "Адрес политики задаётся при сборке", Toast.LENGTH_LONG).show()
            } else {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(BuildConfig.PRIVACY_POLICY_URL)))
            }
        }
        findViewById<Button>(R.id.disconnectButton).setOnClickListener {
            if (!prefs.getBoolean(AppPrefs.PAIRED, false)) {
                Toast.makeText(this, "Устройство не подключено", Toast.LENGTH_SHORT).show()
            } else {
                MaterialAlertDialogBuilder(this)
                    .setTitle("Отключить устройство?")
                    .setMessage("Push-токен будет удалён с сервера. Для повторного подключения понадобится новый код из VK-бота.")
                    .setNegativeButton("Отмена", null)
                    .setPositiveButton("Отключить") { _, _ -> disconnectDevice() }
                    .show()
            }
        }
    }

    private fun disconnectDevice() {
        val prefs = AppPrefs.of(this)
        val json = JSONObject()
            .put("device_id", prefs.getLong("device_id", 0L))
            .put("device_secret", prefs.getString("device_secret", "").orEmpty())
        if (BuildConfig.SERVER_URL.contains("CHANGE-ME")) return
        val request = Request.Builder()
            .url(BuildConfig.SERVER_URL.trimEnd('/') + "/api/driver-app/unregister")
            .post(json.toString().toRequestBody("application/json".toMediaType()))
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) = runOnUiThread {
                Toast.makeText(
                    this@MainActivity,
                    "Не удалось отключить устройство. Проверьте интернет.",
                    Toast.LENGTH_LONG,
                ).show()
            }

            override fun onResponse(call: Call, response: Response) {
                response.use {
                    runOnUiThread {
                        if (it.isSuccessful) {
                            prefs.edit()
                                .putBoolean(AppPrefs.PAIRED, false)
                                .remove("work_pending")
                                .remove(AppPrefs.DRIVER)
                                .remove("device_id")
                                .remove("device_secret")
                                .apply()
                            stopService(
                                Intent(this@MainActivity, AlarmService::class.java)
                                    .setAction("STOP")
                            )
                            recreate()
                        } else {
                            Toast.makeText(
                                this@MainActivity,
                                "Сервер отклонил запрос: ${it.code}",
                                Toast.LENGTH_LONG,
                            ).show()
                        }
                    }
                }
            }
        })
    }

    private fun applyTheme(theme: String) {
        val prefs = AppPrefs.of(this)
        if (prefs.getString(AppPrefs.THEME, "dark") == theme) return
        prefs.edit().putString(AppPrefs.THEME, theme).apply()
        AppCompatDelegate.setDefaultNightMode(
            if (theme == "light") AppCompatDelegate.MODE_NIGHT_NO
            else AppCompatDelegate.MODE_NIGHT_YES
        )
        recreate()
    }

    private fun showBackgroundOnboarding() {
        MaterialAlertDialogBuilder(this)
            .setTitle("Не пропускайте заявки")
            .setMessage(
                "Разрешите уведомления и снимите системное ограничение батареи для БЕЗТАКСЫ. " +
                    "Приложение не работает постоянно: оно просыпается только при push-уведомлении."
            )
            .setNegativeButton("Позже", null)
            .setPositiveButton("Настроить") { _, _ ->
                requestNotificationPermission(openBatteryAfter = true)
            }
            .show()
    }

    private fun requestNotificationPermission(openBatteryAfter: Boolean) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            openBatteryAfterPermission = openBatteryAfter
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else if (openBatteryAfter) {
            openBatterySettings()
        }
    }

    private fun openNotificationSettings() {
        val intent = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
        runCatching { startActivity(intent) }
            .onFailure { openAppDetails() }
    }

    private fun openBatterySettings() {
        val intent = Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
        runCatching { startActivity(intent) }
            .onFailure { openAppDetails() }
    }

    private fun openAppDetails() {
        startActivity(
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.parse("package:$packageName")
            }
        )
    }

    private fun refreshReadiness() {
        if (!::notificationStatus.isInitialized) return
        val notificationsReady = NotificationManagerCompat.from(this).areNotificationsEnabled()
        val power = getSystemService(PowerManager::class.java)
        val backgroundReady = power.isIgnoringBatteryOptimizations(packageName)
        notificationStatus.text = if (notificationsReady) {
            "●  Уведомления разрешены"
        } else {
            "●  Уведомления запрещены"
        }
        notificationStatus.setTextColor(
            ContextCompat.getColor(this, if (notificationsReady) R.color.app_positive else R.color.app_danger)
        )
        backgroundStatus.text = if (backgroundReady) {
            "●  Фоновая работа без ограничений"
        } else {
            "●  Система может ограничивать фон"
        }
        backgroundStatus.setTextColor(
            ContextCompat.getColor(this, if (backgroundReady) R.color.app_positive else R.color.app_warning)
        )
    }

    private fun renderTestButton() {
        if (!::testButton.isInitialized) return
        testButton.text = if (testing) "ОСТАНОВИТЬ ЗВУК" else "ТЕСТ ЗВУКА"
    }

    private fun renderNotificationState() {
        if (!::toggleNotifications.isInitialized) return
        val enabled = AppPrefs.of(this).getBoolean(AppPrefs.NOTIFICATIONS, true)
        connectionText.text = if (enabled) "Уведомления включены" else "Уведомления выключены"
        statusDot.text = if (enabled) "●  Подключено" else "●  Приостановлено"
        statusDot.setTextColor(
            ContextCompat.getColor(this, if (enabled) R.color.app_positive else R.color.app_text_secondary)
        )
        toggleNotifications.text = if (enabled) "ВЫКЛЮЧИТЬ УВЕДОМЛЕНИЯ" else "ВКЛЮЧИТЬ УВЕДОМЛЕНИЯ"
    }

    override fun onResume() {
        super.onResume()
        refreshReadiness()
    }

    override fun onDestroy() {
        if (receiverRegistered) unregisterReceiver(alarmReceiver)
        super.onDestroy()
    }
}
