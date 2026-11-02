package ru.taxi.driveralerts

import android.content.Context
import android.content.SharedPreferences

object AppPrefs {
    private const val FILE = "cfg"
    const val PAIRED = "paired"
    const val DRIVER = "driver"
    const val NOTIFICATIONS = "notifications_enabled"
    const val VIBRATION = "vibration_enabled"
    const val VOLUME = "alert_volume"
    const val THEME = "theme"
    const val ONBOARDING_SHOWN = "background_onboarding_shown"
    const val SCENARIO_ORDER = "scenario_new_order"
    const val SCENARIO_PARALLEL = "scenario_parallel_order"
    const val SCENARIO_CANCELLED = "scenario_cancelled"

    fun of(context: Context): SharedPreferences =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun isScenarioEnabled(prefs: SharedPreferences, event: String?): Boolean =
        when (event) {
            "new_order", "customer_price", "dispatcher_order" ->
                prefs.getBoolean(SCENARIO_ORDER, true)
            "parallel_order" -> prefs.getBoolean(SCENARIO_PARALLEL, true)
            "cancelled" -> prefs.getBoolean(SCENARIO_CANCELLED, true)
            else -> true
        }
}