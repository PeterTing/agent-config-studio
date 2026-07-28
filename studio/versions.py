"""Version ordering, in one place.

Three modules each had their own copy of this, and they disagreed: fixing
the prerelease bug in one left the other two ranking `1.0.0-beta.1` above
`1.0.0`, so a toolkit update was still suppressed and a stable-version
migration still skipped. Duplicated comparison logic diverges silently,
because nothing fails - the wrong answer just looks like no update.
"""

from __future__ import annotations

import re as _re


def version_key(v: str) -> tuple:
    """Order versions the way semver does, including prereleases.

    The previous key split on every separator and mapped non-numeric parts to
    -1, which made `1.0.0-beta.1` sort *above* `1.0.0`: a stable release read as
    "not newer" than the prerelease it superseded, so those updates were never
    offered. It also collapsed alpha, beta and rc to the same value.

    Semver rules encoded here: build metadata is ignored; a release outranks any
    prerelease of the same version; prerelease identifiers compare piecewise,
    numeric before alphanumeric.
    """
    import re as _re

    text = v.strip().lstrip("vV").split("+", 1)[0]  # build metadata is not ordering
    core, _, pre = text.partition("-")
    numbers = tuple(int(p) if p.isdigit() else 0 for p in core.split(".") if p != "")

    if not pre:
        # 1 sorts above 0, so any release outranks every prerelease of it.
        return (numbers, 1, ())

    parts: tuple = ()
    for token in _re.split(r"[.\-]", pre):
        if token == "":
            continue
        # (0, n, "") for numeric identifiers, (1, 0, text) for alphanumeric:
        # semver orders numeric identifiers below alphanumeric ones.
        parts += ((0, int(token), "") if token.isdigit() else (1, 0, token),)
    return (numbers, 0, parts)
