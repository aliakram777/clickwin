import az.naymera.app.data.AiMode
import az.naymera.app.data.ApiFailure
import az.naymera.app.data.ClientRoute
import az.naymera.app.data.LocalState
import az.naymera.app.data.NotConfiguredLocal
import az.naymera.app.data.OnDeviceLocal
import az.naymera.app.data.decideRoute
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class ClientFallbackTest {
    private fun device(state: LocalState) = object : OnDeviceLocal {
        override val state = state
        override suspend fun generate(prompt: String) = "cihaz"
    }

    @Test fun serverIsUsedWhenItAnswers() {
        for (mode in AiMode.values()) assertEquals(ClientRoute.Server, decideRoute(mode, null, NotConfiguredLocal))
    }

    @Test fun connectivityLossFallsBackToOnDeviceOnlyWhenAModelIsReady() {
        for (f in listOf(ApiFailure.Offline, ApiFailure.Timeout)) {
            assertEquals(ClientRoute.OnDevice, decideRoute(AiMode.Auto, f, device(LocalState.Ready)))
            assertEquals(ClientRoute.OnDevice, decideRoute(AiMode.Local, f, device(LocalState.Ready)))
            assertEquals(ClientRoute.OnDevice, decideRoute(AiMode.Auto, f, device(LocalState.Simulation)))
            assertEquals(ClientRoute.Unavailable, decideRoute(AiMode.Auto, f, NotConfiguredLocal)) // model yoxdur: real imkan kimi göstərilmir
            assertEquals(ClientRoute.Unavailable, decideRoute(AiMode.Local, f, NotConfiguredLocal))
        }
    }

    @Test fun cloudModeNeverFallsBackSilently() {
        for (f in listOf(ApiFailure.Offline, ApiFailure.Timeout)) assertEquals(ClientRoute.Unavailable, decideRoute(AiMode.Cloud, f, device(LocalState.Ready)))
    }

    @Test fun nonConnectivityFailuresNeverTriggerAFallback() {
        for (f in listOf(ApiFailure.Unauthorized, ApiFailure.Forbidden, ApiFailure.Conflict, ApiFailure.RateLimited, ApiFailure.Server,
            ApiFailure.Unavailable, ApiFailure.Unknown)) {
            for (mode in AiMode.values()) assertEquals("$f/$mode", ClientRoute.Unavailable, decideRoute(mode, f, device(LocalState.Ready)))
        }
    }

    @Test fun notConfiguredLocalRefusesToGenerate() {
        assertEquals(LocalState.NotConfigured, NotConfiguredLocal.state)
        try { kotlinx.coroutines.runBlocking { NotConfiguredLocal.generate("x") }; fail("istisna gözlənilirdi") } catch (_: IllegalStateException) {}
        assertTrue(AiMode.fromWire("auto") == AiMode.Auto && AiMode.fromWire("zzz") == null)
    }
}
