package az.naymera.app.domain

/** Naviqasiya xəritəsi (Android-dan asılı deyil): 7 təyinat; alt panel 5, qalanları «Daha çox» menyusunda. */
enum class Destination(val route: String, val primary: Boolean) {
    Home("home", true), Assistant("assistant", true), Goals("goals", true), Approvals("approvals", true), Memory("memory", false),
    Journal("journal", false), Settings("settings", true);

    companion object {
        val bottomBar: List<Destination> = listOf(Home, Assistant, Goals, Approvals, Settings)
        val overflow: List<Destination> = entries.filter { it !in bottomBar }
        const val START = "home"
        const val GOAL_DETAIL = "goals/{id}"
        fun goalDetail(id: String): String { require(Regex("[A-Za-z0-9_.:\\-]{1,64}").matches(id)); return "goals/$id" }
        fun fromRoute(route: String?): Destination? = entries.firstOrNull { route == it.route || route?.startsWith(it.route + "/") == true }
        /** Alt paneldə hansı tab seçili görünür (detal ekranı öz bölməsini seçili saxlayır; overflow təyinatlar heç birini). */
        fun selectedTab(route: String?): Destination? = fromRoute(route)?.takeIf { it in bottomBar }
    }
}
