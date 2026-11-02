package ru.taxi.driveralerts
import org.junit.Assert.*
import org.junit.Test
class RouteProgressTest {
    @Test fun coincident() {
        val p=RouteProgress.Point(58.3747,58.3213)
        assertEquals(0.0,RouteProgress.distance(p,p),0.001)
    }
    @Test fun regionalDistance() {
        assertTrue(RouteProgress.distance(RouteProgress.Point(58.3747,58.3213),RouteProgress.Point(58.43,58.24)) in 7000.0..8500.0)
    }
    @Test fun projectsOntoRoad() {
        val route=listOf(RouteProgress.Point(58.0,58.0),RouteProgress.Point(58.01,58.0))
        val match=RouteProgress.nearest(RouteProgress.Point(58.005,58.0001),route)!!
        assertTrue(match.distance in 5.0..7.0);assertTrue(match.along in 550.0..565.0)
    }
    @Test fun emptyAndDegenerate() {
        val p=RouteProgress.Point(58.0,58.0)
        assertNull(RouteProgress.nearest(p,emptyList()))
        assertEquals(0.0,RouteProgress.nearest(p,listOf(p,p))!!.distance,0.001)
    }
    @Test fun doesNotExtendPastSegment() {
        val route=listOf(RouteProgress.Point(58.0,58.0),RouteProgress.Point(58.01,58.0))
        val match=RouteProgress.nearest(RouteProgress.Point(58.02,58.0),route)!!
        assertTrue(match.distance>1000);assertTrue(match.along in 1100.0..1120.0)
    }
}
