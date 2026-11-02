package ru.taxi.driveralerts

import kotlin.math.*

/** Pure, testable geometry. Coordinates are always latitude, longitude. */
object RouteProgress {
    data class Point(val lat: Double, val lon: Double)
    data class Match(val segment: Int, val distance: Double, val along: Double)
    fun distance(a: Point, b: Point): Double {
        val p1 = Math.toRadians(a.lat); val p2 = Math.toRadians(b.lat)
        val h = sin((p2-p1)/2).pow(2) + cos(p1)*cos(p2)*sin(Math.toRadians(b.lon-a.lon)/2).pow(2)
        return 6371000*2*asin(sqrt(h.coerceIn(0.0, 1.0)))
    }
    fun nearest(p: Point, route: List<Point>): Match? {
        if (route.size < 2) return null
        val scaleX = 111195.0*cos(Math.toRadians(p.lat)); val scaleY = 111195.0
        var best = Match(0, Double.POSITIVE_INFINITY, 0.0); var travelled = 0.0
        for (i in 0 until route.lastIndex) {
            val a = route[i]; val b = route[i+1]
            val ax=(a.lon-p.lon)*scaleX; val ay=(a.lat-p.lat)*scaleY
            val dx=(b.lon-a.lon)*scaleX; val dy=(b.lat-a.lat)*scaleY
            val len2=dx*dx+dy*dy
            val t=if (len2==0.0) 0.0 else (-(ax*dx+ay*dy)/len2).coerceIn(0.0, 1.0)
            val d=hypot(ax+t*dx, ay+t*dy); val len=distance(a,b)
            if (d<best.distance) best=Match(i, d, travelled+t*len)
            travelled+=len
        }
        return best
    }
}
