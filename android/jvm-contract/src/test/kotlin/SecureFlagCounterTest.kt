import az.naymera.app.domain.SecureFlagCounter
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** FLAG_SECURE yarış vəziyyəti: ekranlar arası keçiddə köhnə ekranın dispose-u yeni ekranın bayrağını silməməlidir. */
class SecureFlagCounterTest {
    @After fun cleanup() = SecureFlagCounter.reset()

    @Test fun flagIsSetOnFirstAcquireAndClearedOnlyOnLastRelease() {
        SecureFlagCounter.reset()
        assertTrue(SecureFlagCounter.acquire())          // ilk təhlükəsiz ekran: bayraq qoyulur
        assertFalse(SecureFlagCounter.acquire())         // ikinci (keçid zamanı): yenidən qoyulmur
        assertFalse(SecureFlagCounter.release())         // köhnə ekran bağlandı: bayraq QALIR
        assertEquals(1, SecureFlagCounter.count())
        assertTrue(SecureFlagCounter.release())          // son ekran bağlandı: bayraq götürülür
        assertEquals(0, SecureFlagCounter.count())
    }

    @Test fun extraReleaseNeverGoesNegativeAndNextAcquireStillSetsTheFlag() {
        SecureFlagCounter.reset()
        assertFalse(SecureFlagCounter.release())
        assertEquals(0, SecureFlagCounter.count())
        assertTrue(SecureFlagCounter.acquire())
    }

    @Test fun overlappingTransitionOldDisposeAfterNewAcquire() {
        SecureFlagCounter.reset()
        assertTrue(SecureFlagCounter.acquire())          // GoalDetail
        assertFalse(SecureFlagCounter.acquire())         // Approvals başlayır
        assertFalse(SecureFlagCounter.release())         // GoalDetail dispose: bayraq silinməməlidir
        assertEquals(1, SecureFlagCounter.count())
    }
}
