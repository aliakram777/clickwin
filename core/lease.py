"""Lease heartbeat (Mərhələ 18.8): uzun çəkən addımın lease-ini icra müddətində yeniləyir ki, canlı addım səhvən UNKNOWN olmasın.
Yenilənmə yalnız addım hələ RUNNING və eyni op_id ilə olduqda uğurludur; UNKNOWN-a keçibsə yenilənmir və heartbeat dayanır (sahib həllini gözləyir). Daemon ip; xətalar udulur (icranı pozmur)."""
from __future__ import annotations

import threading


class LeaseKeeper:
    def __init__(self, store, plan_id: str, version: int, step_id: str, op_id: str, lease_s: float, interval_s: float | None = None) -> None:
        self._store, self._key, self._lease = store, (plan_id, version, step_id, op_id), float(lease_s)
        self._interval = max(0.05, float(interval_s if interval_s is not None else lease_s / 3))
        self._stop, self._thread = threading.Event(), None
        self.renewals = 0

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._store.renew_lease(*self._key, self._lease):
                    return
                self.renewals += 1
            except Exception:                                    # noqa: BLE001
                return

    def __enter__(self) -> "LeaseKeeper":
        self._thread = threading.Thread(target=self._run, daemon=True, name="lease-keeper")
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
