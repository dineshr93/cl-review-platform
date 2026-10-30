"""WCAG contrast for the diff-paint, meter and kbd pairs used by the console.

Run after changing any colour: ``python3 tools/contrast_check.py``. Every pair must clear
AA (4.5:1 for normal text, 3:1 for large). The hex values mirror the custom properties in
``clreview/web/style.css`` — retoken a colour there and change it here too, or this check
starts certifying colours the app no longer uses.
"""


def lum(hexs):
    h = hexs.lstrip('#')
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


PAIRS = [
    ("diff-del light", "#a7271f", "#fceceb"),
    ("diff-del dark", "#ff9c95", "#401714"),
    ("diff-add light", "#14613c", "#e2f1e9"),
    ("diff-add dark", "#7fdcaa", "#17341f"),
    ("meter-track light vs accent fill", "#245bdb", "#e8ebef"),
    ("ws-count muted on panel light", "#4e5969", "#ffffff"),
    ("ws-count muted on panel dark", "#b4c1d1", "#1d242e"),
    ("kbd ink on code-surface light", "#1b2330", "#f0f2f5"),
    ("kbd ink on code-surface dark", "#e4e9f0", "#252d38"),
]
for name, fg, bg in PAIRS:
    r = ratio(fg, bg)
    aa_normal = r >= 4.5
    aa_large = r >= 3.0
    print(f"{name:38s} {fg} on {bg}  = {r:5.2f}  AA-normal {'PASS' if aa_normal else 'FAIL'}"
          f"  AA-large {'PASS' if aa_large else 'FAIL'}")
