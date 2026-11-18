"""Meyar əsaslı yoxlayıcı (Mərhələ 18.4): `core.Verifier` portu. Addımın `verification_criteria` siyahısı MAŞINLA yoxlanan formatdadırsa yoxlanır; əks halda addım «yoxlanmış» SAYILMIR (insan/sahib yoxlaması lazımdır).
Format: `nonempty`, `contains:<mətn>`, `notcontains:<mətn>`, `maxlen:<N>`, `status:succeeded`. Yoxlama yalnız (sirrsiz, ≤500 simvol) xülasə üzərindədir. Yoxlayıcı icra etmir, model çağırmır."""
from __future__ import annotations

import unicodedata

from core.ports import StepOutcome, StepRequest, VerifyResult

_SIMPLE = {"nonempty", "status:succeeded"}


def is_machine_checkable(criterion: str) -> bool:
    c = criterion.strip()
    if c in _SIMPLE:
        return True
    head, _, arg = c.partition(":")
    return (head in ("contains", "notcontains") and bool(arg)) or (head == "maxlen" and arg.isdigit() and 0 < int(arg) <= 100000)


class CriteriaVerifier:
    def verify(self, request: StepRequest, outcome: StepOutcome) -> VerifyResult:
        text = unicodedata.normalize("NFC", outcome.summary or "")
        failed, unchecked = [], []
        for c in request.step.verification_criteria:
            c = c.strip()
            if not is_machine_checkable(c):
                unchecked.append(c[:40])
                continue
            head, _, arg = c.partition(":")
            ok = {"nonempty": bool(text.strip()), "status:succeeded": outcome.status == "succeeded", "contains": arg in text if head == "contains" else False,
                  "notcontains": arg not in text if head == "notcontains" else False, "maxlen": len(text) <= int(arg) if head == "maxlen" else False}[c if c in _SIMPLE else head]
            if not ok:
                failed.append(c[:40])
        if failed:
            return VerifyResult(False, "failed:" + ";".join(failed))
        if unchecked:
            return VerifyResult(False, "unverifiable:" + ";".join(unchecked))         # maşınla yoxlanmır ⇒ avtomatik «verified» YOXDUR
        return VerifyResult(True, "all_criteria_passed")
