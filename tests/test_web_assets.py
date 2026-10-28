"""Static checks over the shipped web assets - plain Python, no browser, no JS runner."""
from __future__ import annotations

import os
import re
import unittest

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "clreview", "web")

CALL = re.compile(r"([A-Za-z_$][\w$.]*)\s*\(|(\))")
SAFE_WRAPPERS = {"esc", "toast", "dash"}


def sources() -> list[tuple[str, str]]:
    out = []
    for name in sorted(os.listdir(WEB)):
        if name.endswith((".js", ".html")):
            with open(os.path.join(WEB, name), encoding="utf-8") as fh:
                out.append((name, fh.read()))
    return out


def enclosing_calls(src: str) -> dict[int, str | None]:
    """For every offset, the name of the call whose argument list contains it."""
    stack: list[str] = []
    out: dict[int, str | None] = {}
    for m in CALL.finditer(src):
        if m.group(2):
            if stack:
                stack.pop()
        else:
            stack.append(m.group(1).split(".")[-1])
        out[m.end()] = stack[-1] if stack else None
    return out


def _enclosing(stack: dict[int, str | None], pos: int) -> str | None:
    """The innermost call open at ``pos`` (walk back to the last paren boundary seen)."""
    best = None
    for start in sorted((k for k in stack if k <= pos), reverse=True):
        best = stack[start]
        break
    return best


class EscapingTests(unittest.TestCase):
    """`CL.dash` renders an empty-state token; it does not escape. Only `CL.esc` escapes.

    A dash value interpolated into `innerHTML` unescaped is an injection point for whatever a
    scanner, a registry or a connector put in the data. The two legitimate uses are inside
    `CL.esc(...)` and inside `CL.toast(...)`, which writes `textContent`.
    """

    def _unwrapped(self) -> list[str]:
        bad = []
        for name, src in sources():
            stack = enclosing_calls(src)
            for m in re.finditer(r"CL\.dash\(", src):
                if _enclosing(stack, m.start()) in SAFE_WRAPPERS:
                    continue
                line = src[: m.start()].count("\n") + 1
                bad.append(f"{name}:{line}: {src.splitlines()[line - 1].strip()[:100]}")
        return bad

    def test_no_unescaped_dash_in_a_template(self):
        self.assertEqual(self._unwrapped(), [], "CL.dash reached innerHTML without CL.esc")

    def test_the_check_itself_catches_a_violation(self):
        # a guard that cannot fail guards nothing
        broken = 'el.innerHTML = `<td>${CL.dash(row.purl)}</td>`;'
        wrapped = 'el.innerHTML = `<td>${CL.esc(CL.dash(row.purl))}</td>`; CL.toast(CL.dash(x));'
        stack = enclosing_calls(broken)
        self.assertIsNone(_enclosing(stack, broken.index("CL.dash(")))
        stack = enclosing_calls(wrapped)
        self.assertEqual(_enclosing(stack, wrapped.index("CL.dash(", wrapped.index("CL.esc"))), "esc")
        self.assertEqual(_enclosing(stack, wrapped.index("CL.dash(x)")), "toast")

    def test_connector_answered_field_is_escaped(self):
        # `api_status` is whatever the PL tool answered; every other cell on that row is escaped.
        bad = []
        for name, src in sources():
            for n, line in enumerate(src.splitlines(), 1):
                for m in re.finditer(r"\$\{[^}]*i\.api_status[^}]*\}", line):
                    if "CL.esc(" not in m.group(0):
                        bad.append(f"{name}:{n}: {m.group(0)}")
        self.assertEqual(bad, [], "a raw connector field is interpolated into innerHTML")


class RenderedIdsTests(unittest.TestCase):
    """The smoke test drives these ids; a renderer rename must not silently drop one."""

    REQUIRED = ("q-search", "q-clear", "q-apply", "q-sort", "dp-report", "dp-run", "dp-preview",
                "job-detail", "reviewer", "reviewer-error", "toast", "note-body", "add-note")

    def test_every_smoke_id_is_still_rendered(self):
        joined = "\n".join(src for _, src in sources())
        missing = [i for i in self.REQUIRED if f'id="{i}"' not in joined and f"id='{i}'" not in joined]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
