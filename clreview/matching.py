"""Component matching across a project-level and a source-level inventory.

Names are compared with one rule, ``domain.name_key``: the name's letter runs and its version
(``libpng16`` 1.6.37 and ``libpng`` 1.6.37 share {libpng} and 1.6.37).

Hierarchy (highest confidence first):
1. exact normalized purl
2. same name text + same version
3. shared strong identifier
4. same name text, version absent on one side
5. SL name text a strict subset of PL's (``zlib`` vs ``zlib-ng``) + same version - a suggestion
6. same name text, different versions - a suggestion with the version difference as the reason

Levels 5 and 6 always require reviewer confirmation. Nothing here merges components: it proposes
matches with rationale. ``SLScope`` answers, before a PL component is read in detail, whether any
level could pair it with the SL side at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .domain import ComponentData, NameKey, external_ids, name_key, name_relation, norm_purl, norm_version

AUTO_MATCH_METHODS = {"purl", "name_version", "strong_id", "name_only"}


def version_similarity(a: str | None, b: str | None) -> float:
    """0..1: 1 for equal versions, else the share of leading version tokens (1.6 vs 1.6.37) they agree on."""
    na, nb = norm_version(a), norm_version(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = re.findall(r"[a-z]+|\d+", na), re.findall(r"[a-z]+|\d+", nb)
    common = 0
    for x, y in zip(ta, tb):
        if x != y:
            break
        common += 1
    return common / max(len(ta), len(tb), 1)


@dataclass
class MatchResult:
    pl_key: str
    sl_key: str
    method: str
    confidence: float
    rationale: list[str] = field(default_factory=list)
    mismatch_reasons: list[str] = field(default_factory=list)
    status: str = "auto"  # auto | suggested | unmatched
    pl_name: str = ""
    pl_version: str | None = None
    sl_name: str = ""
    sl_version: str | None = None


def _ids(component: ComponentData) -> set[str]:
    return set(external_ids(component.raw)) | set(external_ids(component.metadata))


def _key(c: ComponentData) -> NameKey:
    return name_key(c.name, c.version)


def _words(key: NameKey) -> str:
    return " ".join(sorted(key[0]))


def _name_version_reasons(pl: ComponentData, sl: ComponentData) -> list[str]:
    reasons: list[str] = []
    pk, sk = _key(pl), _key(sl)
    if pk[0] != sk[0]:
        reasons.append(f"name differs: PL {pl.name!r} vs SL {sl.name!r}")
    if pk[1] != sk[1]:
        reasons.append(f"version differs: PL {pl.version!r} vs SL {sl.version!r}")
    return reasons


def _make(plc: ComponentData, slc: ComponentData, method: str, confidence: float, rationale: list[str],
          status: str = "auto") -> MatchResult:
    return MatchResult(
        pl_key=plc.external_id,
        sl_key=slc.external_id,
        method=method,
        confidence=round(confidence, 2),
        rationale=rationale,
        mismatch_reasons=_name_version_reasons(plc, slc),
        status=status,
        pl_name=plc.name,
        pl_version=plc.version,
        sl_name=slc.name,
        sl_version=slc.version,
    )


def _unmatched_pl(plc: ComponentData) -> MatchResult:
    return MatchResult(pl_key=plc.external_id, sl_key="", method="unmatched", confidence=0.0,
                       rationale=["no SL counterpart found"], status="unmatched",
                       pl_name=plc.name, pl_version=plc.version)


def _unmatched_sl(slc: ComponentData) -> MatchResult:
    return MatchResult(pl_key="", sl_key=slc.external_id, method="unmatched", confidence=0.0,
                       rationale=["no PL counterpart found"], status="unmatched",
                       sl_name=slc.name, sl_version=slc.version)


def match_components(pl: list[ComponentData], sl: list[ComponentData],
                     blocked: set[tuple[str, str]] | None = None) -> list[MatchResult]:
    """Propose matches; a ``blocked`` (pl_key, sl_key) pair is one a reviewer rejected and is never proposed."""
    used_pl: set[str] = set()
    used_sl: set[str] = set()
    results: list[MatchResult] = []

    def take(plc: ComponentData, slc: ComponentData, method: str, confidence: float, rationale: list[str],
             status: str = "auto") -> None:
        if blocked and (plc.external_id, slc.external_id) in blocked:
            return
        used_pl.add(plc.external_id)
        used_sl.add(slc.external_id)
        results.append(_make(plc, slc, method, confidence, rationale, status))

    _purl(pl, sl, used_pl, used_sl, take)
    _name_version(pl, sl, used_pl, used_sl, take)
    _strong_id(pl, sl, used_pl, used_sl, take)
    _name_only(pl, sl, used_pl, used_sl, take)
    _name_subset(pl, sl, used_pl, used_sl, take)
    _version_differs(pl, sl, used_pl, used_sl, take)

    results.extend(_unmatched_pl(c) for c in pl if c.external_id not in used_pl)
    results.extend(_unmatched_sl(c) for c in sl if c.external_id not in used_sl)
    return results


def _purl(pl, sl, used_pl, used_sl, take) -> None:
    sl_by_purl: dict[str, list[ComponentData]] = {}
    for c in sl:
        p = norm_purl(c.purl)
        if p:
            sl_by_purl.setdefault(p, []).append(c)
    for plc in pl:
        p = norm_purl(plc.purl)
        if not p or plc.external_id in used_pl:
            continue
        for slc in sl_by_purl.get(p, []):
            if slc.external_id in used_sl:
                continue
            take(plc, slc, "purl", 0.99, [f"identical purl: {p}"])
            break


def _name_version(pl, sl, used_pl, used_sl, take) -> None:
    sl_by_key: dict[NameKey, list[ComponentData]] = {}
    for c in sl:
        if c.external_id in used_sl:
            continue
        key = _key(c)
        if key[0] and key[1]:
            sl_by_key.setdefault(key, []).append(c)
    for plc in pl:
        if plc.external_id in used_pl:
            continue
        key = _key(plc)
        for slc in sl_by_key.get(key, []):
            if slc.external_id in used_sl:
                continue
            take(plc, slc, "name_version", 0.95, [f"name text {{{_words(key)}}} and version {key[1]} are identical"])
            break


def _strong_id(pl, sl, used_pl, used_sl, take) -> None:
    sl_by_id: dict[str, list[ComponentData]] = {}
    for c in sl:
        if c.external_id in used_sl:
            continue
        for i in _ids(c):
            sl_by_id.setdefault(i, []).append(c)
    for plc in pl:
        if plc.external_id in used_pl:
            continue
        for i in _ids(plc):
            for slc in sl_by_id.get(i, []):
                if slc.external_id in used_sl:
                    continue
                take(plc, slc, "strong_id", 0.9, [f"shared identifier: {i}"])
                break
            if plc.external_id in used_pl:
                break


def _name_only(pl, sl, used_pl, used_sl, take) -> None:
    sl_by_text: dict[frozenset[str], list[ComponentData]] = {}
    for c in sl:
        if c.external_id in used_sl:
            continue
        text = _key(c)[0]
        if text:
            sl_by_text.setdefault(text, []).append(c)
    for plc in pl:
        if plc.external_id in used_pl:
            continue
        pk = _key(plc)
        candidates = [c for c in sl_by_text.get(pk[0], []) if c.external_id not in used_sl]
        if len(candidates) != 1:
            continue
        sk = _key(candidates[0])
        if not pk[1] or not sk[1]:
            take(plc, candidates[0], "name_only", 0.72, ["name text matches; version absent on one side"])
        elif pk[1] == sk[1]:
            take(plc, candidates[0], "name_only", 0.72, ["name text matches; versions agree"])


def _name_subset(pl, sl, used_pl, used_sl, take) -> None:
    """SL ``zlib`` 1.2 for PL ``zlib-ng`` 1.2: plausible, never certain - a suggestion."""
    sl_by_version: dict[str, list[tuple[ComponentData, NameKey]]] = {}
    for c in sl:
        if c.external_id not in used_sl:
            key = _key(c)
            if key[0] and key[1]:
                sl_by_version.setdefault(key[1], []).append((c, key))
    for plc in pl:
        if plc.external_id in used_pl:
            continue
        pk = _key(plc)
        options = [(c, k) for c, k in sl_by_version.get(pk[1], [])
                   if c.external_id not in used_sl and name_relation(pk, k) == "subset"]
        if not options:
            continue
        slc, sk = max(options, key=lambda o: len(o[1][0]))  # the most specific SL name wins
        share = len(sk[0]) / len(pk[0])
        take(plc, slc, "name_subset", round(0.50 + 0.15 * share, 2),
             [f"SL name text {{{_words(sk)}}} is part of PL {{{_words(pk)}}}",
              f"version {pk[1]} is identical", "reviewer confirmation required"],
             status="suggested")


def _version_differs(pl, sl, used_pl, used_sl, take) -> None:
    """The same component at another version: the version gap is the finding, so it is proposed."""
    sl_by_text: dict[frozenset[str], list[tuple[ComponentData, NameKey]]] = {}
    for c in sl:
        if c.external_id not in used_sl:
            key = _key(c)
            if key[0]:
                sl_by_text.setdefault(key[0], []).append((c, key))
    for plc in pl:
        if plc.external_id in used_pl:
            continue
        pk = _key(plc)
        options = [(c, k) for c, k in sl_by_text.get(pk[0], []) if c.external_id not in used_sl]
        if not options:
            continue
        slc, sk = max(options, key=lambda o: version_similarity(pk[1], o[1][1]))
        vsim = version_similarity(pk[1], sk[1])
        take(plc, slc, "version_differs", round(min(0.69, 0.45 + 0.2 * vsim), 2),
             [f"name text {{{_words(pk)}}} is identical",
              f"version similarity {vsim:.2f}: PL {plc.version!r} vs SL {slc.version!r}",
              "reviewer confirmation required"],
             status="suggested")


class SLScope:
    """Which PL components the SL side could pair with, decided from identity alone.

    A refresh lists the PL inventory cheaply and reads details (origins, copyrights) only for what
    this admits: the same name text at any version, an SL name that is a subset at the same
    version, a shared purl or identifier, or a pair a reviewer has already linked (``keep``).
    It mirrors the levels of ``match_components``, so nothing it rejects could have matched.
    """

    def __init__(self, sl: Iterable[ComponentData], keep: Iterable[str] = ()):
        self.keys: list[NameKey] = []
        self.purls: set[str] = set()
        self.ids: set[str] = set()
        for c in sl:
            key = _key(c)
            if key[0]:
                self.keys.append(key)
            if norm_purl(c.purl):
                self.purls.add(norm_purl(c.purl))
            self.ids |= _ids(c)
        self.texts = {k[0] for k in self.keys}
        self.keep = set(keep)
        self.size = len(self.keys)
        self.skipped: list[tuple[str, str, str | None]] = []  # (key, name, version) of what was left out

    def admits(self, name: str, version: str | None, *, key: str = "", purl: str | None = None,
               ids: Iterable[str] = ()) -> bool:
        """Whether to read this component; a rejected one with a ``key`` is recorded in ``skipped``."""
        if self._admits(name, version, key, purl, ids):
            return True
        if key:
            self.skipped.append((key, name, version))
        return False

    def _admits(self, name: str, version: str | None, key: str, purl: str | None, ids: Iterable[str]) -> bool:
        if key and key in self.keep:
            return True
        if norm_purl(purl) and norm_purl(purl) in self.purls:
            return True
        if self.ids and self.ids.intersection(ids):
            return True
        pk = name_key(name, version)
        if pk[0] in self.texts:
            return True
        return any(k[1] and k[1] == pk[1] and name_relation(pk, k) == "subset" for k in self.keys)

    def admits_component(self, c: ComponentData) -> bool:
        return self.admits(c.name, c.version, key=c.external_id, purl=c.purl, ids=_ids(c))
