import az.naymera.app.domain.Destination
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class NavigationTest {
    @Test fun allSevenDestinationsExistWithUniqueRoutesAndStartIsHome() {
        assertEquals(setOf("home", "assistant", "goals", "approvals", "memory", "journal", "settings"), Destination.entries.map { it.route }.toSet())
        assertEquals(7, Destination.entries.map { it.route }.distinct().size)
        assertEquals(Destination.Home, Destination.fromRoute(Destination.START))
    }

    @Test fun bottomBarHasAtMostFiveAndEveryDestinationIsReachable() {
        assertTrue(Destination.bottomBar.size <= 5)
        assertEquals(Destination.entries.toSet(), (Destination.bottomBar + Destination.overflow).toSet())
        assertTrue(Destination.bottomBar.containsAll(listOf(Destination.Home, Destination.Assistant, Destination.Goals, Destination.Approvals)))
        assertTrue(Destination.Memory in Destination.overflow && Destination.Journal in Destination.overflow)
    }

    @Test fun goalDetailRouteKeepsGoalsTabSelectedAndRejectsUnsafeIds() {
        assertEquals("goals/goal_abc123", Destination.goalDetail("goal_abc123"))
        assertEquals(Destination.Goals, Destination.selectedTab("goals/goal_abc123"))
        assertNull(Destination.selectedTab("journal")); assertNull(Destination.selectedTab(null))
        for (bad in listOf("", "a/b", "x y", "../etc", "a".repeat(65))) {
            try { Destination.goalDetail(bad); fail("rədd olunmalı idi: $bad") } catch (_: IllegalArgumentException) {}
        }
        assertNull(Destination.fromRoute("unknown"))
    }
}
