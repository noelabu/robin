"""Go RE2 pattern syntax that Python's `re` doesn't accept, translated (gitleaks rules are written for Go).

Go allows inline flags such as `(?i)` anywhere; they apply from that point to the end of the enclosing group,
across any `|` alternatives that follow. Python only accepts them at the start of a pattern, so a mid-pattern
`(?flags)` becomes a scoped group `(?flags:...)`, closed before each `|` and reopened after it (so the
alternation keeps its shape and the flags still carry over), and closed where the enclosing group ends.
Go's `\\z` (end of text) is Python's `\\Z`.
"""

import re

_FLAGS = re.compile(r"\(\?([imsU]+)\)")


def go_to_python(pattern: str) -> str:
    out: list[str] = []
    # Scoped flag groups open at each nesting level; level 0 is the whole pattern.
    levels: list[list[str]] = [[]]
    i, n = 0, len(pattern)
    in_class = False
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            esc = pattern[i : i + 2]
            out.append("\\Z" if esc == "\\z" and not in_class else esc)
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            out.append(ch)
            i += 1
            continue
        if ch == "[":
            in_class = True
            out.append(ch)
            i += 1
            if i < n and pattern[i] == "^":
                out.append("^")
                i += 1
            if i < n and pattern[i] == "]":  # a leading ] is literal
                out.append("]")
                i += 1
            continue
        m = _FLAGS.match(pattern, i)
        if m:
            flags = m.group(1).replace("U", "")
            if i == 0:
                out.append(f"(?{flags})" if flags else "")
            elif flags:
                out.append(f"(?{flags}:")
                levels[-1].append(flags)
            i = m.end()
            continue
        if ch == "(":
            levels.append([])
            out.append(ch)
            i += 1
            continue
        if ch == ")" and len(levels) > 1:
            out.append(")" * len(levels.pop()))
            out.append(")")
            i += 1
            continue
        if ch == "|" and levels[-1]:
            open_here = levels[-1]
            out.append(")" * len(open_here))
            out.append("|")
            out.extend(f"(?{f}:" for f in open_here)  # the flags still apply after the |
            i += 1
            continue
        out.append(ch)
        i += 1
    out.append(")" * len(levels[0]))
    return "".join(out)
