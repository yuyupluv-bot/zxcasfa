package ru.taxi.driveralerts

import android.Manifest
import android.annotation.SuppressLint
import android.content.*
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Color
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.net.Uri
import android.os.*
import android.speech.tts.TextToSpeech
import android.view.View
import android.widget.*
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import okhttp3.Call
import org.json.JSONArray
import org.json.JSONObject
import org.maplibre.android.MapLibre
import org.maplibre.android.camera.CameraPosition
import org.maplibre.android.geometry.LatLng
import org.maplibre.android.maps.MapView
import org.maplibre.android.maps.MapLibreMap
import org.maplibre.android.maps.Style
import org.maplibre.android.style.layers.CircleLayer
import org.maplibre.android.style.layers.LineLayer
import org.maplibre.android.style.layers.PropertyFactory.*
import org.maplibre.android.style.sources.GeoJsonSource
import java.time.Instant
import java.util.Locale
import java.util.UUID

/** Test-stage work screen. Bot/DB, not the phone, owns the queue and timers. */
class DriverActivity : AppCompatActivity(), LocationListener {
    private val handler=Handler(Looper.getMainLooper())
    private lateinit var api: DriverApi
    private lateinit var mapView: MapView
    private var map: MapLibreMap?=null
    private lateinit var status: TextView
    private lateinit var mapStatus: TextView
    private lateinit var directions: TextView
    private lateinit var card: LinearLayout
    private lateinit var timers: TextView
    private lateinit var gps: LocationManager
    private var state: JSONObject?=null
    private var revision=""
    private var foreground=false
    private var stateCall: Call?=null
    private var clockOffset=0L
    private var pending: JSONObject?=null
    private var lastFix: Location?=null
    private var gpsListening=false
    private var points: List<RouteProgress.Point> = emptyList()
    private var target: RouteProgress.Point?=null
    private var lastRouteAt=0L
    private var routeBusy=false
    private var offRouteCount=0
    private var steps: List<JSONObject> = emptyList()
    private var stepDistances: List<Pair<JSONObject,Double>> = emptyList()
    private var spoken=""
    private var speechReady=false
    private lateinit var speech: TextToSpeech
    private val lime=Color.rgb(184,238,69)
    private val ink=Color.rgb(24,35,23)
    private val permission=registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        startGps()
    }
    private val refresh=object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) { loadState() }
    }
    private val poll=object : Runnable {
        override fun run() {
            if (!foreground) return
            loadState(); renderTimers()
            handler.postDelayed(this, if (pending!=null || (state?.optJSONArray("orders")?.length() ?: 0)>0) 2500 else 8000)
        }
    }
    private val ticker=object : Runnable {
        override fun run() { if (foreground) { renderTimers(); handler.postDelayed(this,1000) } }
    }
    private fun dp(n: Int)=(n*resources.displayMetrics.density).toInt()
    private fun label(text: String, size: Float=16f)=TextView(this).apply {
        this.text=text; textSize=size; setTextColor(ink); setPadding(dp(16),dp(8),dp(16),dp(8))
    }
    private fun button(text: String, action: () -> Unit)=Button(this).apply {
        this.text=text; isAllCaps=false; minHeight=dp(48); setTextColor(ink)
        backgroundTintList=ColorStateList.valueOf(lime); setOnClickListener { action() }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!AppPrefs.of(this).getBoolean(AppPrefs.PAIRED,false)) {
            startActivity(Intent(this,MainActivity::class.java)); finish(); return
        }
        MapLibre.getInstance(this)
        pending=runCatching { JSONObject(AppPrefs.of(this).getString("work_pending",null) ?: "") }.getOrNull()
        api=DriverApi(this); gps=getSystemService(LOCATION_SERVICE) as LocationManager
        speech=TextToSpeech(this) { result -> speechReady=result==TextToSpeech.SUCCESS; if (speechReady) speech.language=Locale("ru","RU") }
        val root=LinearLayout(this).apply { orientation=LinearLayout.VERTICAL; setBackgroundColor(Color.WHITE) }
        // Android 15 draws edge-to-edge: reserve system bars on API 35+ too.
        root.setOnApplyWindowInsetsListener { v, i ->
            @Suppress("DEPRECATION") v.setPadding(i.systemWindowInsetLeft,i.systemWindowInsetTop,i.systemWindowInsetRight,i.systemWindowInsetBottom); i
        }
        val header=LinearLayout(this).apply { orientation=LinearLayout.HORIZONTAL; gravity=android.view.Gravity.CENTER_VERTICAL }
        status=label("Подключение…",18f)
        header.addView(status,LinearLayout.LayoutParams(0,dp(64),1f))
        header.addView(button("Настройки") { startActivity(Intent(this,MainActivity::class.java).putExtra("settings_only",true)) })
        root.addView(header)
        mapStatus=label("Карта района загружается…",14f); root.addView(mapStatus)
        mapView=MapView(this); mapView.onCreate(savedInstanceState)
        root.addView(mapView,LinearLayout.LayoutParams(-1,0,1f))
        directions=label("Горнозаводск · Пашия · Кусья · 50 км",16f); root.addView(directions)
        timers=label("",14f); root.addView(timers)
        val scroller=ScrollView(this)
        card=LinearLayout(this).apply { orientation=LinearLayout.VERTICAL }
        scroller.addView(card); root.addView(scroller,LinearLayout.LayoutParams(-1,dp(280)))
        setContentView(root)
        mapView.getMapAsync { m ->
            map=m
            m.cameraPosition=CameraPosition.Builder().target(LatLng(58.3747,58.3213)).zoom(12.0).build()
            m.setStyle(Style.Builder().fromUri(BuildConfig.SERVER_URL.trimEnd('/')+"/driver-map/style.json")) { style ->
                mapStatus.text="© OpenStreetMap contributors · карта района"
                style.addSource(GeoJsonSource("driver-position", """{"type":"FeatureCollection","features":[]}"""))
                style.addLayer(CircleLayer("driver-position-dot","driver-position").withProperties(circleRadius(8f),circleColor(ink),circleStrokeWidth(3f),circleStrokeColor(lime)))
                style.addSource(GeoJsonSource("driver-route", """{"type":"FeatureCollection","features":[]}"""))
                style.addLayerBelow(LineLayer("driver-route-line","driver-route").withProperties(lineColor("#497516"),lineWidth(6f)),"driver-position-dot")
                lastFix?.let { updateMarker(it) }; drawRoute()
            }
            mapView.addOnDidFailLoadingMapListener { mapStatus.text="Карта недоступна. Заявки работают отдельно." }
            m.addOnMapLongClickListener { ll -> confirmDestination(RouteProgress.Point(ll.latitude,ll.longitude),"Точка на карте"); true }
        }
        card.addView(label("Загрузка состояния водителя…"))
    }
    override fun onStart() {
        super.onStart(); if (!::mapView.isInitialized) return
        if (!AppPrefs.of(this).getBoolean(AppPrefs.PAIRED,false)) {
            startActivity(Intent(this,MainActivity::class.java)); finish(); return
        }
        mapView.onStart(); foreground=true
        disableActions(); revision=""
        ContextCompat.registerReceiver(this,refresh,IntentFilter("ru.taxi.driveralerts.REFRESH"),ContextCompat.RECEIVER_NOT_EXPORTED)
        handler.post(poll); handler.post(ticker); startGps()
    }
    override fun onResume() { super.onResume(); if (::mapView.isInitialized) mapView.onResume() }
    override fun onPause() { if (::mapView.isInitialized) mapView.onPause(); super.onPause() }
    override fun onStop() {
        foreground=false; handler.removeCallbacksAndMessages(null)
        if (::api.isInitialized) api.cancel(); stateCall=null; routeBusy=false
        disableActions()
        if (::gps.isInitialized && gpsListening) { gps.removeUpdates(this); gpsListening=false }
        runCatching { unregisterReceiver(refresh) }
        if (::mapView.isInitialized) mapView.onStop()
        super.onStop()
    }
    override fun onDestroy() {
        if (::speech.isInitialized) { speech.stop(); speech.shutdown() }
        if (::mapView.isInitialized) mapView.onDestroy()
        super.onDestroy()
    }
    override fun onLowMemory() { super.onLowMemory(); if (::mapView.isInitialized) mapView.onLowMemory() }
    override fun onSaveInstanceState(outState: Bundle) { super.onSaveInstanceState(outState); if (::mapView.isInitialized) mapView.onSaveInstanceState(outState) }
    private fun loadState() {
        if (!foreground || stateCall!=null) return
        stateCall=api.post("/api/driver-app/work/state") { code, result -> runOnUiThread {
            stateCall=null
            if (!foreground) return@runOnUiThread
            if (code!=200 || result==null) {
                status.text=if (code==401 || code==403) "Доступ не подтверждён" else "Нет связи · данные устарели"
                disableActions(); return@runOnUiThread
            }
            state=result
            clockOffset=runCatching { Instant.parse(result.getString("server_time")).toEpochMilli()-SystemClock.elapsedRealtime() }.getOrDefault(clockOffset)
            val commands=result.optJSONArray("commands") ?: JSONArray()
            val unresolved=pending
            if (unresolved!=null) {
                for (i in 0 until commands.length()) {
                    val c=commands.getJSONObject(i)
                    if (c.optString("request_id")==unresolved.optString("request_id") && c.optString("status") in listOf("done","rejected","failed")) {
                        if (c.optString("status")!="done") toast(c.optString("error","Действие не выполнено"))
                        pending=null; AppPrefs.of(this).edit().remove("work_pending").apply()
                    }
                }
                // Never repeat a mutation automatically when its network outcome is unknown.
            }
            val driver=result.optJSONObject("driver") ?: JSONObject()
            status.text=if (!result.optBoolean("bot_online")) "Бот недоступен" else if (driver.optBoolean("online")) "На линии · ${driver.optString("line", "—")}" else "Вы не на линии"
            if (revision!=result.optString("revision") || unresolved!=pending || card.childCount==1 || card.tag!=result.optBoolean("can_act")) {
                revision=result.optString("revision"); render(result)
            }
            if (!result.optBoolean("can_act")) disableActions()
            renderTimers()
        } }
    }
    private fun render(s: JSONObject) {
        card.tag=s.optBoolean("can_act")
        card.removeAllViews()
        val d=s.optJSONObject("driver") ?: JSONObject()
        if (!d.isNull("queue_position")) card.addView(label("Очередь: №${d.optInt("queue_position")}",18f))
        val orders=s.optJSONArray("orders") ?: JSONArray()
        for (i in 0 until orders.length()) {
            val o=orders.getJSONObject(i)
            card.addView(label("Заявка №${o.optString("number")} · ${mapOf("searching" to "Предложение", "assigned" to "Подача", "arrived" to "На месте", "in_progress" to "Поездка", "parallel_assigned" to "Параллельная")[o.optString("status")] ?: "Заявка"}",18f))
            val from=o.optString("from"); val to=o.optString("to")
            card.addView(label(if (from.isBlank() && to.isBlank()) o.optString("route") else "$from → $to"))
            if (o.optString("comment").isNotBlank()) card.addView(label(o.optString("comment")))
            if (o.optString("price").isNotBlank()) card.addView(label("Стоимость: ${o.optString("price")} ₽"))
            val row=LinearLayout(this)
            row.addView(button("К подаче") { searchAddress(from) },LinearLayout.LayoutParams(0,-2,1f))
            row.addView(button("К назначению") { searchAddress(to) },LinearLayout.LayoutParams(0,-2,1f))
            card.addView(row)
            if (o.optBoolean("voice_in_vk")) card.addView(button("Голосовой адрес в VK") { startActivity(Intent(Intent.ACTION_VIEW,Uri.parse(s.optString("vk_url","https://vk.me")))) })
        }
        val notice=s.optString("role_notice").takeUnless { it=="null" || it.isBlank() } ?: s.optString("notice")
        if (notice.isNotBlank()) card.addView(label(notice))
        val actions=s.optJSONArray("actions") ?: JSONArray()
        for (i in 0 until actions.length()) {
            val a=actions.getJSONObject(i)
            val b=button(a.optString("label")) {
                MaterialAlertDialogBuilder(this).setTitle(a.optString("label")).setMessage("Выполнить действие для текущей заявки?")
                    .setNegativeButton("Отмена",null).setPositiveButton("Выполнить") { _,_->sendAction(a.getJSONObject("payload"),"") }.show()
            }
            b.tag="work-action"; b.isEnabled=s.optBoolean("can_act") && pending==null
            if (a.optBoolean("danger")) b.backgroundTintList=ColorStateList.valueOf(Color.rgb(255,222,217))
            card.addView(b)
        }
        if (!s.isNull("input_hint") && s.optString("input_hint").isNotBlank()) {
            val b=button(s.optString("input_hint")) { textDialog(s.optString("input_hint"),"") { sendAction(JSONObject(),it) } }
            b.tag="work-action"; b.isEnabled=s.optBoolean("can_act") && pending==null; card.addView(b)
        }
        val p=pending
        if (p!=null) {
            card.addView(label("Проверяется результат действия. Другие команды временно отключены."))
            card.addView(button("Проверить / повторить тот же запрос") { submit(p) })
        }
        if (target!=null) card.addView(button("Остановить маршрут") {
            target=null; points=emptyList(); steps=emptyList(); stepDistances=emptyList(); speech.stop(); drawRoute()
            window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            directions.text="Маршрут остановлен"; render(s)
        })
    }
    private fun disableActions() { if (!::card.isInitialized) return; for (i in 0 until card.childCount) if (card.getChildAt(i).tag=="work-action") card.getChildAt(i).isEnabled=false }
    private fun renderTimers() {
        val list=state?.optJSONArray("timers") ?: JSONArray(); val now=SystemClock.elapsedRealtime()+clockOffset
        val names=mapOf("accept" to "На принятие", "eta" to "Подача", "prearrival" to "Подача", "parallel_eta" to "Параллельная подача", "waiting" to "Ожидание", "route_parallel_offer" to "Предложение")
        timers.text=(0 until list.length()).mapNotNull { i ->
            val t=list.getJSONObject(i); val deadline=runCatching { Instant.parse(t.getString("deadline")).toEpochMilli() }.getOrNull() ?: return@mapNotNull null
            val seconds=((deadline-now)/1000).coerceAtLeast(0)
            "${names[t.optString("kind")] ?: t.optString("kind")}: ${seconds/60}:${(seconds%60).toString().padStart(2,'0')}"
        }.joinToString(" · ")
    }
    private fun sendAction(payload: JSONObject, text: String) {
        if (pending!=null || state?.optBoolean("can_act")!=true) return
        val command=JSONObject().put("request_id",UUID.randomUUID().toString()).put("revision",revision).put("payload",payload).put("text",text)
        pending=command
        // Stored body contains no device secret; after process death resolve the same UUID.
        AppPrefs.of(this).edit().putString("work_pending",command.toString()).apply()
        state?.let { render(it) }; submit(command)
    }
    private fun submit(command: JSONObject) {
        api.post("/api/driver-app/work/action",JSONObject(command.toString())) { code, result -> runOnUiThread {
            if (!foreground) return@runOnUiThread
            if (code in listOf(400,401,403,409,429) || (code==503 && result?.optString("error")=="bot_unavailable_or_wrong_role")) {
                pending=null; AppPrefs.of(this).edit().remove("work_pending").apply()
                toast("Действие не принято. Обновите состояние перед повтором.")
                revision=""; loadState()
            } else if (code==0 || code>=500) {
                toast("Ответ не получен. Автоматического повтора не будет."); state?.let { render(it) }
            } else if (result?.optString("status") in listOf("done","rejected","failed")) {
                pending=null; AppPrefs.of(this).edit().remove("work_pending").apply(); revision=""; loadState()
            } else loadState()
        } }
    }
    private fun textDialog(title: String, initial: String, done: (String)->Unit) {
        val input=EditText(this).apply { setText(initial); maxLines=3; setPadding(dp(24),dp(12),dp(24),dp(12)) }
        MaterialAlertDialogBuilder(this).setTitle(title).setView(input).setNegativeButton("Отмена",null)
            .setPositiveButton("Далее") { _,_->val value=input.text.toString().trim(); if (value.isNotBlank()) done(value.take(500)) }.show()
    }
    private fun searchAddress(initial: String) {
        textDialog("Уточните населённый пункт и адрес",initial) { query ->
            api.post("/api/driver-app/geo/search",JSONObject().put("query",query)) { code, result -> runOnUiThread {
                if (!foreground) return@runOnUiThread
                val rows=result?.optJSONArray("results") ?: JSONArray()
                if (code!=200 || rows.length()==0) { toast("Адрес не найден. Уточните его или удерживайте точку на карте."); return@runOnUiThread }
                val labels=Array(rows.length()) { rows.getJSONObject(it).optString("label") }
                MaterialAlertDialogBuilder(this).setTitle("Выберите точный адрес").setItems(labels) { _,i ->
                    val xy=rows.getJSONObject(i).getJSONArray("point")
                    confirmDestination(RouteProgress.Point(xy.getDouble(0),xy.getDouble(1)),labels[i])
                }.setNegativeButton("Отмена",null).show()
            } }
        }
    }
    private fun confirmDestination(p: RouteProgress.Point, name: String) {
        MaterialAlertDialogBuilder(this).setTitle("Построить маршрут?").setMessage("$name\n${"%.5f".format(p.lat)}, ${"%.5f".format(p.lon)}\nПроверьте точку: адрес из заявки не определяется автоматически.")
            .setPositiveButton("Построить") { _,_->target=p; requestRoute() }.setNegativeButton("Отмена",null).show()
    }
    private fun requestRoute() {
        val fix=lastFix; val to=target ?: return
        if (fix==null || SystemClock.elapsedRealtimeNanos()-fix.elapsedRealtimeNanos>30_000_000_000L || fix.accuracy>50) { toast("Нужно точное и свежее местоположение GPS"); return }
        if (routeBusy) return
        routeBusy=true; lastRouteAt=SystemClock.elapsedRealtime(); directions.text="Построение маршрута по дорогам…"
        val body=JSONObject().put("from",JSONArray(listOf(fix.latitude,fix.longitude))).put("to",JSONArray(listOf(to.lat,to.lon)))
        api.post("/api/driver-app/geo/route",body) { code, result -> runOnUiThread {
            routeBusy=false
            if (!foreground || target!=to) return@runOnUiThread
            val route=result?.optJSONObject("route")
            if (code!=200 || route==null) { points=emptyList(); steps=emptyList(); drawRoute(); directions.text="Маршрут недоступен. Заявка продолжает работать."; return@runOnUiThread }
            val coords=route.optJSONObject("geometry")?.optJSONArray("coordinates") ?: JSONArray()
            points=(0 until coords.length()).map { val c=coords.getJSONArray(it); RouteProgress.Point(c.getDouble(1),c.getDouble(0)) }
            val legs=route.optJSONArray("legs") ?: JSONArray(); val all=mutableListOf<JSONObject>()
            for (i in 0 until legs.length()) { val ss=legs.getJSONObject(i).optJSONArray("steps") ?: JSONArray(); for (j in 0 until ss.length()) all.add(ss.getJSONObject(j)) }
            steps=all
            // OSRM maneuver nodes are also in its full route geometry. Index once,
            // rather than scanning the whole polyline for every step on each GPS fix.
            val positions=HashMap<Pair<Long,Long>,MutableList<Double>>()
            var along=0.0
            for (i in points.indices) {
                if (i>0) along+=RouteProgress.distance(points[i-1],points[i])
                val p=points[i];val key=Pair(kotlin.math.round(p.lat*1e6).toLong(),kotlin.math.round(p.lon*1e6).toLong())
                positions.getOrPut(key) { mutableListOf() }.add(along)
            }
            var previous=0.0
            stepDistances=steps.mapNotNull { step ->
                val xy=step.optJSONObject("maneuver")?.optJSONArray("location") ?: return@mapNotNull null
                val key=Pair(kotlin.math.round(xy.getDouble(1)*1e6).toLong(),kotlin.math.round(xy.getDouble(0)*1e6).toLong())
                val at=positions[key]?.firstOrNull { it>=previous-1 } ?: return@mapNotNull null
                previous=at; Pair(step,at)
            }
                window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            spoken=""; offRouteCount=0; drawRoute()
            directions.text="${"%.1f".format(route.optDouble("distance")/1000)} км · ~${(route.optDouble("duration")/60).toInt()} мин · без пробок"
            map?.animateCamera(org.maplibre.android.camera.CameraUpdateFactory.newLatLngZoom(LatLng(fix.latitude,fix.longitude),15.0))
        } }
    }
    @SuppressLint("MissingPermission") private fun startGps() {
        if (!foreground || gpsListening) return
        if (ContextCompat.checkSelfPermission(this,Manifest.permission.ACCESS_FINE_LOCATION)!=PackageManager.PERMISSION_GRANTED) {
            if (!AppPrefs.of(this).getBoolean("gps_requested",false)) {
                AppPrefs.of(this).edit().putBoolean("gps_requested",true).apply()
                MaterialAlertDialogBuilder(this).setTitle("Геопозиция для карты").setMessage("Положение используется на этом экране для движения по карте и маршрута. В этой тестовой версии при сворачивании навигация останавливается.")
                    .setPositiveButton("Разрешить") { _,_->permission.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION,Manifest.permission.ACCESS_COARSE_LOCATION)) }.setNegativeButton("Не сейчас",null).show()
            } else mapStatus.text="Для маршрута разрешите точную геопозицию в настройках Android."
            return
        }
        runCatching { gps.requestLocationUpdates(LocationManager.GPS_PROVIDER,2000L,5f,this); gpsListening=true }
            .onFailure { mapStatus.text="GPS недоступен. Приём заявок работает." }
    }
    override fun onLocationChanged(location: Location) {
        if (!foreground) return
        lastFix=location; updateMarker(location)
        if (points.isEmpty() || location.accuracy>50) return
        val p=RouteProgress.Point(location.latitude,location.longitude)
        val match=RouteProgress.nearest(p,points) ?: return
        if (match.distance>60) offRouteCount++ else offRouteCount=0
        if (offRouteCount>=3 && SystemClock.elapsedRealtime()-lastRouteAt>20_000) { requestRoute(); return }
        val destination=target
        if (destination!=null && RouteProgress.distance(p,destination)<35) {
            directions.text="Вы рядом с точкой. Статус заявки меняется только вашей кнопкой."
            return
        }
        // Use route progress, not straight-line proximity, to avoid crossing-road prompts.
        val next=stepDistances.mapNotNull { (step,along) ->
            val ahead=along-match.along
            if (ahead>10) Pair(step,ahead) else null
        }.minByOrNull { it.second } ?: return
        val maneuver=next.first.optJSONObject("maneuver") ?: JSONObject()
        val modifier=maneuver.optString("modifier")
        val instruction=when {
            maneuver.optString("type")=="arrive" -> "Прибытие к точке"
            maneuver.optString("type").contains("roundabout") -> "Круговое движение, съезд ${next.first.optInt("exit",maneuver.optInt("exit",1))}"
            modifier.contains("uturn") -> "Разворот"
            modifier.contains("left") -> "Поверните налево"
            modifier.contains("right") -> "Поверните направо"
            else -> "Продолжайте движение"
        }
        directions.text="Через ${next.second.toInt()} м · $instruction\n${next.first.optString("name")}"
        val key=next.first.toString()
        if (next.second<200 && key!=spoken && speechReady) {
            spoken=key; speech.speak("Через ${next.second.toInt()} метров. $instruction",TextToSpeech.QUEUE_FLUSH,null,key)
        }
    }
    private fun updateMarker(location: Location) {
        val json="""{"type":"Feature","geometry":{"type":"Point","coordinates":[${location.longitude},${location.latitude}]},"properties":{}}"""
        map?.style?.getSourceAs<GeoJsonSource>("driver-position")?.setGeoJson(json)
    }
    private fun drawRoute() {
        val json=if (points.size<2) """{"type":"FeatureCollection","features":[]}""" else JSONObject().put("type","Feature").put("properties",JSONObject()).put("geometry",JSONObject().put("type","LineString").put("coordinates",JSONArray(points.map { listOf(it.lon,it.lat) }))).toString()
        map?.style?.getSourceAs<GeoJsonSource>("driver-route")?.setGeoJson(json)
    }
    private fun toast(message: String) { Toast.makeText(this,message,Toast.LENGTH_LONG).show() }
}
