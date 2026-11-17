import az.naymera.app.security.Messages
import org.junit.Assert.assertEquals
import org.junit.Test

/** Dillərarası vektorlar: Python `AuthService.pair_message` / `step_up_message` ilə eyni olmalıdır (tests/test_api_auth.py). */
class MessagesTest {
    @Test
    fun pairMessageMatchesTheServerFormat() {
        assertEquals("naymera-pair|ABCD2345", String(Messages.pairMessage("ABCD2345"), Charsets.UTF_8))
    }

    @Test
    fun stepUpMessageMatchesTheServerFormat() {
        val hash = "ab".repeat(32)
        val msg = Messages.challengeMessage("c1", "n1", "approval", "id1", hash, "approve")
        assertEquals("naymera-step-up|c1|n1|approval|id1|$hash|approve", String(msg, Charsets.UTF_8))
    }

    @Test
    fun resumeMessageAllowsAnEmptyPayloadHash() {
        val msg = Messages.challengeMessage("c", "n", "resume", "stop", "", "approve")
        assertEquals("naymera-step-up|c|n|resume|stop||approve", String(msg, Charsets.UTF_8))
    }
}
