# Keep only names that are accessed by platform/Firebase reflection.
-keep class ru.taxi.driveralerts.PushService { *; }
-keep class ru.taxi.driveralerts.AlarmService { *; }
-dontwarn org.conscrypt.**