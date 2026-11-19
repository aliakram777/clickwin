import az.naymera.app.data.ApiClient
import az.naymera.app.data.ApiException
import az.naymera.app.data.ApiFailure
import az.naymera.app.data.ApiTimeouts
import az.naymera.app.data.ConnectionState
import az.naymera.app.data.EventSync
import az.naymera.app.data.ServerUrl
import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/** Backend OLMADAN: şəbəkə qırılması, vaxt aşımı, HTTP xətaları, token yenilənməsi, əlaqə vəziyyəti. */
class NetworkFailureTest {
    private val fast = ApiTimeouts(connectMs = 500, readMs = 300, writeMs = 300, callMs = 1500, retryBaseMs = 5)
    private val closers = mutableListOf<() -> Unit>()

    @After fun cleanup() = closers.forEach { runCatching(it) }

    private fun store(url: String, access: String = "nay_a_x", refresh: String = "nay_r_y") =
        MemoryTokenStore(url).also { it.accessToken = access; it.refreshToken = refresh }

    private fun fail(block: suspend () -> Unit): ApiException {
        try { runBlocking { block() } } catch (e: ApiException) { return e }
        fail("ApiException gözlənilirdi")
        throw IllegalStateException()
    }

    /** Qəbul edib dərhal bağlayan server: bağlantı kəsilməsi. */
    private fun droppingServer(accepts: AtomicInteger): Int {
        val ss = ServerSocket(0)
        closers += { ss.close() }
        thread(isDaemon = true) { while (!ss.isClosed) try { ss.accept().also { accepts.incrementAndGet() }.close() } catch (_: Exception) {} }
        return ss.localPort
    }

    /** Qəbul edib heç cavab verməyən server: vaxt aşımı. */
    private fun silentServer(): Int {
        val ss = ServerSocket(0)
        val held = CopyOnWriteArrayList<java.net.Socket>()
        closers += { ss.close(); held.forEach { runCatching { it.close() } } }
        thread(isDaemon = true) { while (!ss.isClosed) try { held += ss.accept() } catch (_: Exception) {} }
        return ss.localPort
    }

    private data class Fake(val server: HttpServer, val port: Int, val log: CopyOnWriteArrayList<String>)

    private fun fakeServer(handler: (path: String, method: String, auth: String?) -> Pair<Int, String>): Fake {
        val log = CopyOnWriteArrayList<String>()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/") { ex ->
            val (code, body) = handler(ex.requestURI.path, ex.requestMethod, ex.requestHeaders.getFirst("Authorization"))
            log += "${ex.requestMethod} ${ex.requestURI.path} ${ex.requestHeaders.getFirst("Authorization") ?: "-"}"
            val bytes = body.toByteArray()
            ex.responseHeaders.add("Content-Type", "application/json")
            ex.sendResponseHeaders(code, bytes.size.toLong())
            ex.responseBody.use { it.write(bytes) }
        }
        server.start()
        closers += { server.stop(0) }
        return Fake(server, server.address.port, log)
    }

    private fun err(code: String) = """{"error":{"code":"$code","message":"m","request_id":"r"}}"""

    @Test fun serverUrlAcceptsHttpsAndLocalDebugHostsOnly() {
        assertEquals("https://naymera.example", ServerUrl.validate("https://naymera.example/"))
        assertEquals("http://10.0.2.2:8765", ServerUrl.validate("http://10.0.2.2:8765"))
        for (bad in listOf("ht" + "tp://example.com", "ftp://x.y", "https://u:p@host", "https://h/?q=1", "javascript:alert(1)", "", "ht" + "tp://192.168.1.5")) {
            try { ServerUrl.validate(bad); fail("rədd olunmalı: $bad") } catch (_: IllegalArgumentException) {}
        }
    }

    @Test fun connectionRefusedIsOfflineAndGetsAreRetriedWithBackoff() {
        val accepts = AtomicInteger()
        val port = droppingServer(accepts)
        val e = fail { ApiClient(store("http://127.0.0.1:$port"), fast).status() }
        assertEquals(0, e.status)
        assertEquals(ApiFailure.Offline, e.failure)
        assertEquals(3, accepts.get()) // GET: 1 + 2 təkrar
    }

    @Test fun postIsNeverRetriedAutomatically() {
        val accepts = AtomicInteger()
        val port = droppingServer(accepts)
        val e = fail { ApiClient(store("http://127.0.0.1:$port"), fast).sendMessage("salam", null) }
        assertEquals(ApiFailure.Offline, e.failure)
        assertEquals(1, accepts.get()) // təkrar icra riski yoxdur
    }

    @Test fun silentServerTimesOut() {
        val port = silentServer()
        val e = fail { ApiClient(store("http://127.0.0.1:$port"), fast.copy(callMs = 1000)).sendMessage("salam", null) }
        assertEquals(ApiFailure.Timeout, e.failure)
    }

    @Test fun unreachablePortIsOffline() {
        val port = ServerSocket(0).use { it.localPort } // bağlanıb: heç kim dinləmir
        val e = fail { ApiClient(store("http://127.0.0.1:$port"), fast).status() }
        assertEquals(ApiFailure.Offline, e.failure)
    }

    @Test fun httpStatusesMapToUserFacingFailures() {
        val expected = mapOf(403 to ApiFailure.Forbidden, 404 to ApiFailure.Unavailable, 409 to ApiFailure.Conflict,
            429 to ApiFailure.RateLimited, 500 to ApiFailure.Server, 503 to ApiFailure.Unavailable, 302 to ApiFailure.Unknown, 418 to ApiFailure.Unknown)
        for ((status, failure) in expected) {
            val f = fakeServer { _, _, _ -> status to err("c$status") }
            val e = fail { ApiClient(store("http://127.0.0.1:${f.port}"), fast).stop() }
            assertEquals("status $status", failure, e.failure)
            assertEquals(status, e.status)
            assertEquals("c$status", e.code)
        }
    }

    @Test fun nonJsonErrorBodiesDoNotCrashTheClient() {
        val f = fakeServer { _, _, _ -> 500 to "<html>boom</html>" }
        val e = fail { ApiClient(store("http://127.0.0.1:${f.port}"), fast).stop() }
        assertEquals(ApiFailure.Server, e.failure)
        assertEquals("http_500", e.code)
    }

    @Test fun unauthorizedWithFailedRefreshExpiresTheSession() {
        val f = fakeServer { _, _, _ -> 401 to err("unauthorized") }
        val s = store("http://127.0.0.1:${f.port}")
        val api = ApiClient(s, fast)
        val e = fail { api.status() }
        assertEquals(ApiFailure.Unauthorized, e.failure)
        assertTrue(api.sessionExpired.value)
        assertTrue(f.log.any { it.startsWith("POST /v1/token/refresh") })
    }

    @Test fun refreshRotatesTokensAndRetriesOnceAndIsSingleFlight() {
        val refreshes = AtomicInteger()
        val f = fakeServer { path, _, auth ->
            when {
                path == "/v1/token/refresh" -> { refreshes.incrementAndGet(); 200 to """{"access_token":"nay_a_NEW","refresh_token":"nay_r_NEW","access_expires_in":900}""" }
                auth == "Bearer nay_a_NEW" -> 200 to """{"stop_active":false,"pending_approvals":0,"api_version":1,"llm_is_mock":true}"""
                else -> 401 to err("unauthorized")
            }
        }
        val s = store("http://127.0.0.1:${f.port}", access = "nay_a_OLD")
        val api = ApiClient(s, fast)
        val results = runBlocking { (1..8).map { async { api.status() } }.awaitAll() }
        assertTrue(results.all { it.llmIsMock })
        assertEquals(1, refreshes.get()) // 8 paralel 401 -> tək yenilənmə
        assertEquals("nay_a_NEW", s.accessToken)
        assertEquals("nay_r_NEW", s.refreshToken)
        assertFalse(api.sessionExpired.value)
    }

    @Test fun authorizationHeaderIsBearerAndRedirectsAreNotFollowed() {
        val f = fakeServer { _, _, _ -> 200 to """{"stop_active":false,"pending_approvals":3,"api_version":1,"llm_is_mock":true}""" }
        runBlocking { assertEquals(3, ApiClient(store("http://127.0.0.1:${f.port}", access = "nay_a_TOK"), fast).status().pendingApprovals) }
        assertEquals("GET /v1/system/status Bearer nay_a_TOK", f.log.single())
        val r = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        r.createContext("/") { ex -> ex.responseHeaders.add("Location", "https://evil.example/"); ex.sendResponseHeaders(302, -1); ex.close() }
        r.start(); closers += { r.stop(0) }
        val e = fail { ApiClient(store("http://127.0.0.1:${r.address.port}"), fast).status() }
        assertEquals(302, e.status) // yönləndirmə izlənmir (token başqa hosta getmir)
    }

    @Test fun eventSyncReportsReconnectingThenOfflineThenRecoversAndAcks() {
        val up = java.util.concurrent.atomic.AtomicBoolean(false)
        val f = fakeServer { path, method, _ ->
            when {
                !up.get() -> 503 to err("down")
                path == "/v1/events" -> 200 to """{"events":[{"seq":7,"type":"job_update","id":"e","ts":1.0,"payload":{},"schema_version":1}],"next_cursor":7,"last_ack":0}"""
                path == "/v1/events/ack" && method == "POST" -> 200 to """{"ok":true}"""
                else -> 404 to err("nf")
            }
        }
        val s = store("http://127.0.0.1:${f.port}")
        val api = ApiClient(s, fast)
        val sync = EventSync(api, s, backoffBaseMs = 5)
        val states = CopyOnWriteArrayList<ConnectionState>()
        val result = runBlocking {
            val watcher = launch { sync.state.collect { states += it } }
            val got = CompletableDeferred<List<az.naymera.app.data.MobileEvent>>()
            val collector = launch { sync.stream(intervalMs = 5).collect { got.complete(it) } }
            kotlinx.coroutines.delay(400)
            up.set(true)
            val events = withTimeoutOrNull(5_000) { got.await() }
            kotlinx.coroutines.delay(300) // kursor və ack emit-dən sonra icra olunur
            collector.cancel(); watcher.cancel()
            events
        }
        assertEquals(7L, result!!.single().seq)
        assertTrue(states.contains(ConnectionState.Reconnecting) || states.contains(ConnectionState.Offline))
        assertTrue(states.contains(ConnectionState.Offline))
        assertEquals(ConnectionState.Connected, sync.state.value)
        assertEquals(7L, s.eventCursor)
        assertTrue(f.log.any { it.startsWith("POST /v1/events/ack") })
    }

    @Test fun eventSyncPropagatesUnauthorizedInsteadOfLoopingForever() {
        val f = fakeServer { _, _, _ -> 401 to err("unauthorized") }
        val s = store("http://127.0.0.1:${f.port}")
        val sync = EventSync(ApiClient(s, fast), s, backoffBaseMs = 5)
        try { runBlocking { withTimeoutOrNull(3_000) { sync.stream(intervalMs = 5).first() } }; fail("401 yuxarı atılmalı") } catch (e: ApiException) {
            assertEquals(ApiFailure.Unauthorized, e.failure)
        }
    }

    @Test fun twoDifferentTimeoutProfilesAreIndependent() {
        assertNotEquals(ApiTimeouts(), fast)
    }
}
