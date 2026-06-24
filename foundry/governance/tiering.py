"""Change-risk tiering — the deterministic triage classifier. (governance.md §1e)

Triage assigns every change a **change-risk tier** (LOW / STANDARD / HIGH) so the
assurance DEPTH and HITL load scale with risk: a one-line fix should not pay a
major feature's nine-gate + signing + human-Release tax, and a one-line APR /
AML-threshold change must NOT be cheap merely because the diff is small.

Load-bearing invariants (governance.md §1e):

* **Tiering NEVER relaxes integrity.** It scales assurance *depth* + *human load*
  only — admission stays fail-closed, verdicts stay runner-signed, and the
  integrity-critical gates run at EVERY tier (a LOW tier still runs Build / Test /
  scoped Security fail-closed). `TierDecision.relaxes_integrity()` is `False` by
  construction and asserted by callers; this module never returns anything else.
* **Fail-closed force-escalation.** Any security-relevant / trust-root path, any
  regulated/domain-pack capability, or ANY ambiguity escalates to HIGH
  (`forced_high=True`). We NEVER silently downgrade — when in doubt, escalate.
* **Deterministic + auditable.** This is a rule-based substring/keyword classifier
  (no model call): the verdict is a pure function of the request text +
  capabilities, so it is stable across a kill-then-resume and reviewable.

The classifier is wired by the supervisor at triage; it stamps the `risk_tier`
single-writer channel (`TierDecision`) and seeds the gate set / HITL depth.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from foundry.schemas.governance import RiskTier, TierDecision
from foundry.state.seq import Seq

# --- signal vocabularies (deterministic; broad on purpose) -------------------
# A false force-escalation costs one cheaper-than-a-breach extra review; a missed
# one ships an un-reviewed trust-root change. So these are deliberately broad and
# the default-on-doubt is HIGH (governance.md §1e).

# Capabilities whose regulatory significance is orthogonal to diff size: a domain
# pack force-escalates the tier + forces-on two-person Release independent of size
# (governance.md §1e / domains.md §4). Mirrors the architecture domain-pack set.
_REGULATED_CAPABILITIES: frozenset[str] = frozenset(
    {"regulated", "fintech", "healthcare", "ml", "data", "privacy", "pii", "phi"}
)

# Security-relevant / trust-root path or config signals — touching ANY forces HIGH
# regardless of diff size (governance.md §1e: build/CI/IaC, deps/lockfile,
# scanner-config, auth/crypto, the trust root, and the gate/oracle config itself).
_SECURITY_TRUST_SIGNALS: tuple[str, ...] = (
    # trust root / config-of-the-checker (mis-edit = silently weakened assurance)
    "trust root",
    "trust-root",
    "trust anchor",
    "gate config",
    "gate set",
    "scanner config",
    "scanner-config",
    "oracle config",
    "oracle binding",
    "verifier",
    "signing key",
    "signer",
    "kms",
    "keystore",
    "secret",
    "credential",
    # auth / crypto
    "auth",
    "authn",
    "authz",
    "login",
    "password",
    "oauth",
    "saml",
    "session token",
    "jwt",
    "crypto",
    "encrypt",
    "decrypt",
    "tls",
    "ssl",
    "certificate",
    "permission",
    "privilege",
    "rbac",
    "acl",
    "sandbox",
    "egress",
    # supply chain / build / CI / IaC / deps (governance.md §1e HIGH triggers)
    "build pipeline",
    "ci/cd",
    "ci pipeline",
    "github actions",
    "workflow",
    "dockerfile",
    "terraform",
    "iac",
    "infrastructure",
    "deploy",
    "dependency",
    "dependencies",
    "lockfile",
    "lock file",
    "package.json",
    "requirements.txt",
    "pyproject",
)

# Abuse / security-relevant feature signals (force HIGH; safety-significant).
_ABUSE_SIGNALS: tuple[str, ...] = (
    "abuse",
    "fraud",
    "exploit",
    "vulnerab",
    "injection",
    "xss",
    "csrf",
    "ssrf",
    "rce",
    "malware",
    "ransom",
    "exfiltrat",
    "bypass",
    "jailbreak",
    "prompt injection",
    "pii",
    "payment",
    "billing",
)

# Trivial / low-blast-radius signals — a LOW tier candidate (lighter assurance
# depth, fewer OPTIONAL gates) PROVIDED no security/regulated signal fired.
_TRIVIAL_SIGNALS: tuple[str, ...] = (
    "typo",
    "comment",
    "docstring",
    "rename",
    "log message",
    "logging message",
    "copy change",
    "wording",
    "readme",
    "documentation",
    "whitespace",
    "formatting",
    "lint",
    "trivial",
)

# Advisory scoped gate families per tier (governance.md §1e table). NEVER a relax
# of the integrity-critical gates — those run at every tier; this only adds/removes
# OPTIONAL depth (Supply-chain / Threat-review at HIGH; a reduced OPTIONAL set at
# LOW). Build / Test / scoped Security run at ALL tiers, fail-closed.
# Strictness order over risk tiers (governance.md §1e). HIGH is strictest. Used by
# the tighten-only floor raise a domain-pack mode/tier veto applies.
_TIER_STRICTNESS: dict[RiskTier, int] = {
    RiskTier.LOW: 0,
    RiskTier.STANDARD: 1,
    RiskTier.HIGH: 2,
}

_INTEGRITY_GATES: tuple[str, ...] = ("build", "test", "security")
_GATE_SETS: dict[RiskTier, tuple[str, ...]] = {
    RiskTier.LOW: _INTEGRITY_GATES,
    RiskTier.STANDARD: (*_INTEGRITY_GATES, "spec", "review", "e2e"),
    RiskTier.HIGH: (
        *_INTEGRITY_GATES,
        "spec",
        "review",
        "e2e",
        "supply_chain",
        "threat_review",
    ),
}


def classify_tier_at_floor(
    source: str | Mapping[str, Any],
    *,
    floor: RiskTier,
    capabilities: Iterable[str] | None = None,
    two_person_release: bool = False,
    seq: Seq,
    run_against: str = "",
) -> TierDecision:
    """Classify ``source`` then TIGHTEN the result up to ``floor`` (tighten-only).

    The domain-pack mode/tier veto can only RAISE the change-risk tier (a regulated
    pack force-escalates to HIGH independent of diff size; governance.md §1e /
    domains.md §4). This wraps :func:`classify_tier` and returns the STRICTER of the
    deterministic verdict and ``floor`` — never the laxer (a pack can never relax the
    classifier's own tier). ``two_person_release`` is OR-ed on (a pack-forced
    segregation-of-duties Release). The returned decision still asserts
    ``relaxes_integrity() is False``.
    """
    base = classify_tier(source, capabilities=capabilities, seq=seq, run_against=run_against)
    if _TIER_STRICTNESS[floor] <= _TIER_STRICTNESS[base.tier] and not two_person_release:
        return base
    tier = base.tier if _TIER_STRICTNESS[base.tier] >= _TIER_STRICTNESS[floor] else floor
    forced_high = base.forced_high or tier is RiskTier.HIGH
    two_person = base.two_person_release or two_person_release
    return _decide(
        tier,
        rationale=(
            f"{base.rationale}; tightened to floor {floor.value} by an engaged "
            "domain pack's mode/tier veto (tighten-only, domains.md §4)"
        ),
        seq=seq,
        run_against=run_against,
        forced_high=forced_high,
        two_person_release=two_person,
    )


def _request_text(source: str | Mapping[str, Any]) -> str:
    """Extract the lowercased request text from a state mapping or a raw str."""
    if isinstance(source, str):
        return source.lower()
    return str(source.get("request") or "").lower()


def _capabilities_of(
    source: str | Mapping[str, Any], capabilities: Iterable[str] | None
) -> frozenset[str]:
    """Union the explicit `capabilities=` arg with any on a state mapping.

    Both are lowercased so the regulated-capability check is case-insensitive.
    """
    caps: set[str] = set()
    if capabilities is not None:
        caps.update(c.lower() for c in capabilities)
    if not isinstance(source, str):
        caps.update(str(c).lower() for c in (source.get("required_capabilities") or ()))
    return frozenset(caps)


def _matched(text: str, signals: Iterable[str]) -> tuple[str, ...]:
    """The signals that appear in `text` (ordered, deterministic)."""
    return tuple(s for s in signals if s in text)


def classify_tier(
    source: str | Mapping[str, Any],
    *,
    capabilities: Iterable[str] | None = None,
    seq: Seq,
    run_against: str = "",
) -> TierDecision:
    """Classify a change into a LOW / STANDARD / HIGH :class:`TierDecision`.

    Deterministic, rule-based, no model call (governance.md §1e). Rules:

    * Any security-relevant / trust-root / gate-oracle-scanner-config path signal,
      any abuse/security-significant signal, OR any regulated/domain-pack
      capability → **FORCE HIGH** (``forced_high=True``); a regulated capability
      additionally sets ``two_person_release=True`` (distinct-identity sign-off,
      governance.md §1e). Force-escalation is fail-closed: when in doubt, HIGH.
    * Otherwise a trivial / low-blast-radius signal → **LOW** (lighter assurance
      depth, fewer OPTIONAL gates — the integrity-critical gates STILL run).
    * Otherwise → **STANDARD** (the safe default: full integrated gate set).

    The tier sets the advisory ``gate_set`` (scoped gate family / depth) and the
    HITL depth, but NEVER relaxes admission/integrity — every returned decision
    has ``relaxes_integrity() is False`` and runs the integrity-critical gates
    fail-closed (a LOW tier just drops OPTIONAL depth, not the integrity floor).
    """
    text = _request_text(source)
    caps = _capabilities_of(source, capabilities)

    regulated_caps = tuple(sorted(caps & _REGULATED_CAPABILITIES))
    security_hits = _matched(text, _SECURITY_TRUST_SIGNALS)
    abuse_hits = _matched(text, _ABUSE_SIGNALS)
    trivial_hits = _matched(text, _TRIVIAL_SIGNALS)

    # --- force-escalation: any security/regulated signal -> HIGH (fail-closed) --
    if regulated_caps or security_hits or abuse_hits:
        reasons: list[str] = []
        if regulated_caps:
            reasons.append(f"regulated/domain-pack capabilities {list(regulated_caps)}")
        if security_hits:
            reasons.append(f"security/trust-root/config signals {list(security_hits)}")
        if abuse_hits:
            reasons.append(f"abuse/security-significant signals {list(abuse_hits)}")
        # Regulated significance forces two-person distinct-identity Release
        # independent of diff size (governance.md §1e / domains.md §4). A
        # trust-root / signer / gate-config touch is treated the same way.
        two_person = bool(regulated_caps) or _touches_trust_root(security_hits)
        return _decide(
            RiskTier.HIGH,
            rationale=(
                "force-escalated to HIGH (fail-closed force-escalation, gov §1e): "
                + "; ".join(reasons)
            ),
            seq=seq,
            run_against=run_against,
            forced_high=True,
            two_person_release=two_person,
        )

    # --- empty / missing request -> STANDARD (never silently downgrade) ---------
    if not text.strip():
        return _decide(
            RiskTier.STANDARD,
            rationale=(
                "no request text to classify — default STANDARD (ambiguity never "
                "silently downgrades below the full integrated gate set, gov §1e)"
            ),
            seq=seq,
            run_against=run_against,
        )

    # --- trivial / low-blast-radius (only after no force signal) -> LOW ---------
    if trivial_hits:
        return _decide(
            RiskTier.LOW,
            rationale=(
                "trivial / low-blast-radius change (no security/trust/regulated "
                f"signal) {list(trivial_hits)} — reduced OPTIONAL assurance depth; "
                "integrity-critical gates still run fail-closed"
            ),
            seq=seq,
            run_against=run_against,
        )

    # --- default -> STANDARD ---------------------------------------------------
    return _decide(
        RiskTier.STANDARD,
        rationale="ordinary app-code change — STANDARD (full integrated gate set)",
        seq=seq,
        run_against=run_against,
    )


# Trust-root sub-signals (subset of `_SECURITY_TRUST_SIGNALS`) whose touch is a
# checker/trust surface mis-edit — these force two-person Release like a regulated
# pack (governance.md §1e: trust-root surface requires two-person sign-off).
_TRUST_ROOT_SIGNALS: frozenset[str] = frozenset(
    {
        "trust root",
        "trust-root",
        "trust anchor",
        "gate config",
        "gate set",
        "scanner config",
        "scanner-config",
        "oracle config",
        "oracle binding",
        "verifier",
        "signing key",
        "signer",
        "kms",
        "keystore",
    }
)


def _touches_trust_root(security_hits: Iterable[str]) -> bool:
    return any(h in _TRUST_ROOT_SIGNALS for h in security_hits)


def _decide(
    tier: RiskTier,
    *,
    rationale: str,
    seq: Seq,
    run_against: str,
    forced_high: bool = False,
    two_person_release: bool = False,
) -> TierDecision:
    """Build the frozen :class:`TierDecision`, defending the never-relax invariant.

    The decision is asserted to never relax integrity here too, so a future edit
    to `TierDecision.relaxes_integrity` can never silently let this module emit an
    integrity-relaxing tier (defense in depth — governance.md §1e).
    """
    decision = TierDecision(
        tier=tier,
        rationale=rationale,
        run_against=run_against,
        gate_set=_GATE_SETS[tier],
        two_person_release=two_person_release,
        forced_high=forced_high,
        seq=seq,
    )
    # Integrity is NEVER relaxed by tiering — assert it at the source.
    assert decision.relaxes_integrity() is False  # load-bearing invariant
    return decision
