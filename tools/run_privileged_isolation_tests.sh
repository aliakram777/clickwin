#!/usr/bin/env bash
# Root tələb edən agent izolyasiya testlərini (unikal uid, şəbəkə namespace-i, seccomp, proses ağacı təmizliyi) ATLAMADAN işlədir.
# İstifadə: tools/run_privileged_isolation_tests.sh [pytest arqumentləri]
# Root olmayan istifadəçi: sudo ilə yenidən çağırır. Atlanan HƏR izolyasiya testi (və ya root olmamaq) sıfırdan fərqli çıxış kodu verir.
# Bu skript Linux x86_64 tələb edir (seccomp profili). GitHub-hosted runner-də `isolation-privileged` işi eyni əmri işlədir (bax: .github/workflows/ci.yml, AGENT_FACTORY.md).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E env "PATH=$PATH" "PYTHON=$PY" "$0" "$@"
fi
export NAYMERA_REQUIRE_PRIVILEGED=1
exec "$PY" -m pytest -q -rs -p no:cacheprovider tests/test_agent_isolation.py tests/test_agent_isolation_hardening.py tests/test_isolation_limits.py tests/test_ci_privileged_policy.py "$@"
