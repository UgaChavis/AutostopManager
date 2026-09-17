"""Deterministic provenance tiers for J1 public-web discovery.

The registry is a collection aid, not a trust decision: every source still
needs document-level relevance and applicability checks.  It is deliberately
small, explicit and importable by both the fetcher and future evidence reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SourceRule:
    """A checked domain classification rule.

    ``tier`` is evidence provenance, not a statement that every page on a
    domain supports a given claim.
    """

    domain: str
    source_class: str
    tier: str
    label: str = ""


@dataclass(frozen=True)
class SourceClassification:
    source_class: str
    source_tier: str
    source_basis: str


# Keep these exact, auditable domain rules rather than guessing authority from
# a page title.  More specific domains must appear before their parent only
# when they intentionally have a different tier/class.
SOURCE_RULES: tuple[SourceRule, ...] = (
    # A: regulators, OEMs, recalls and manufacturer technical systems.
    SourceRule("nhtsa.gov", "official_registry", "A", "US National Highway Traffic Safety Administration"),
    SourceRule("safercar.gov", "official_registry", "A", "US National Highway Traffic Safety Administration"),
    SourceRule("toyota-tech.eu", "official_registry", "A", "Toyota Technical Information System"),
    SourceRule("techinfo.toyota.com", "official_registry", "A", "Toyota Technical Information System"),
    SourceRule("toyota.com", "official_registry", "A", "Toyota"),
    SourceRule("lexus.com", "official_registry", "A", "Lexus"),
    SourceRule("mercedes-benz.com", "official_registry", "A", "Mercedes-Benz"),
    SourceRule("mbusa.com", "official_registry", "A", "Mercedes-Benz USA"),
    SourceRule("bmwgroup.com", "official_registry", "A", "BMW Group"),
    SourceRule("bmw.com", "official_registry", "A", "BMW"),
    SourceRule("audi.com", "official_registry", "A", "Audi"),
    SourceRule("volkswagen.com", "official_registry", "A", "Volkswagen"),
    SourceRule("honda.com", "official_registry", "A", "Honda"),
    SourceRule("ford.com", "official_registry", "A", "Ford"),
    SourceRule("gm.com", "official_registry", "A", "General Motors"),
    SourceRule("stellantis.com", "official_registry", "A", "Stellantis"),
    # B: component makers and technical references.  These are technical
    # sources, not OEM confirmation of vehicle-specific applicability.
    SourceRule("denso.com", "technical", "B"),
    SourceRule("denso-am.eu", "technical", "B"),
    SourceRule("bosch.com", "technical", "B"),
    SourceRule("boschaftermarket.com", "technical", "B"),
    SourceRule("ngkntk.com", "technical", "B"),
    SourceRule("delphiautoparts.com", "technical", "B"),
    SourceRule("continental-aftermarket.com", "technical", "B"),
    SourceRule("mahle-aftermarket.com", "technical", "B"),
    SourceRule("hella.com", "technical", "B"),
    # Mirrors and user-uploaded manual hosts remain useful discovery leads,
    # but cannot inherit the authority of the original document.
    SourceRule("oemdtc.com", "technical_reference", "D"),
    SourceRule("workshop-manuals.com", "technical_reference", "D"),
    SourceRule("manualslib.com", "technical_reference", "D"),
    # C: catalogues can establish a number or a listed fitment, but not a
    # diagnosis or a manufacturer repair procedure.
    SourceRule("autodoc.de", "supplier_catalog", "C"),
    SourceRule("autodoc.ru", "supplier_catalog", "C"),
    SourceRule("exist.ru", "supplier_catalog", "C"),
    SourceRule("emex.ru", "supplier_catalog", "C"),
    SourceRule("partsouq.com", "supplier_catalog", "C"),
    SourceRule("rockauto.com", "supplier_catalog", "C"),
    SourceRule("partsapi.ru", "supplier_catalog", "C"),
    SourceRule("amayama.com", "supplier_catalog", "C"),
    SourceRule("toyodiy.com", "supplier_catalog", "C"),
    # D: owner reports and editorial context are hypotheses/context only.
    SourceRule("drive2.ru", "owner_community", "D"),
    SourceRule("reddit.com", "owner_community", "D"),
    SourceRule("club-lexus.ru", "owner_community", "D"),
    SourceRule("lexusownersclub.co.uk", "owner_community", "D"),
    SourceRule("mbworld.org", "owner_community", "D"),
    SourceRule("benzworld.org", "owner_community", "D"),
    SourceRule("bimmerpost.com", "owner_community", "D"),
    SourceRule("vwvortex.com", "owner_community", "D"),
    SourceRule("caranddriver.com", "editorial", "D"),
    SourceRule("motortrend.com", "editorial", "D"),
    SourceRule("autonews.ru", "editorial", "D"),
    SourceRule("zr.ru", "editorial", "D"),
)


def _same_or_subdomain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith("." + domain)


def _host_and_path(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return "", ""
    return (parsed.hostname or "").casefold().rstrip("."), parsed.path.casefold()


def classify_source(url: str, *, title: str = "", kind: str = "") -> SourceClassification:
    """Classify a discovered public URL before any document fetch.

    Unknown sources intentionally stay unclassified.  Heuristics never assign
    tier A, because only an explicit registry can identify an OEM/regulator.
    """

    host, path = _host_and_path(url)
    if not host:
        return SourceClassification("unknown", "unclassified", "fallback:invalid_url")
    for rule in SOURCE_RULES:
        if not _same_or_subdomain(host, rule.domain):
            continue
        if rule.source_class == "official_registry":
            return SourceClassification(
                rule.source_class,
                rule.tier,
                f"registry:official:{rule.domain}:{rule.label}",
            )
        if rule.source_class == "technical":
            return SourceClassification(rule.source_class, rule.tier, f"registry:technical:{rule.domain}")
        if rule.source_class == "supplier_catalog":
            return SourceClassification(rule.source_class, rule.tier, f"registry:supplier:{rule.domain}")
        if rule.source_class == "owner_community":
            return SourceClassification(rule.source_class, rule.tier, f"registry:owner_community:{rule.domain}")
        if rule.source_class == "technical_reference":
            return SourceClassification(rule.source_class, rule.tier, f"registry:technical_reference:{rule.domain}")
        return SourceClassification(rule.source_class, rule.tier, f"registry:editorial:{rule.domain}")
    if host.startswith(("forum.", "forums.")) or any(token in path for token in ("/forum", "/forums/", "/community/")):
        return SourceClassification("owner_community", "D", "heuristic:community_url")
    if any(token in host for token in ("catalog", "parts", "autoparts")):
        return SourceClassification("supplier_catalog", "C", "heuristic:catalog_hostname")
    title_lower = str(title or "").casefold()
    if str(kind or "").casefold() == "pdf" and any(
        token in (path + " " + title_lower) for token in ("manual", "workshop", "service", "tsb")
    ):
        return SourceClassification("technical", "B", "heuristic:technical_pdf")
    if any(token in host for token in ("news", "media", "journal", "magazine")):
        return SourceClassification("editorial", "D", "heuristic:editorial_hostname")
    return SourceClassification("unknown", "unclassified", "fallback:unclassified")


def discovery_domain(url: str) -> str:
    """Return a stable domain bucket for search-result caps.

    Registry domains group subdomains (for example static.nhtsa.gov and
    www.nhtsa.gov) without requiring a public-suffix dependency.
    """

    host, _ = _host_and_path(url)
    if not host:
        return ""
    for rule in SOURCE_RULES:
        if _same_or_subdomain(host, rule.domain):
            return rule.domain
    return host.removeprefix("www.")


__all__ = ["SOURCE_RULES", "SourceClassification", "SourceRule", "classify_source", "discovery_domain"]
