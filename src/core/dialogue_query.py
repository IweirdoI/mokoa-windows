"""Build retrieval queries from user evidence, without adding assistant answers."""
import re

_CLOSING = re.compile(
    r"(?:no[, ]*)?(?:thanks?(?: you)?(?: very much)?|thank you|"
    r"that(?:'s| is) all(?: i need)?|nothing else|bye|goodbye|"
    r"ok(?:ay)?|great|perfect|yes|no|谢谢|好的|没有了|再见)"
    r"(?:[, .!]*(?:thanks?(?: you)?|thank you|bye|goodbye))*[.!。！ ]*", re.I
)


def task_query(user_turns: list[str], *, max_chars: int = 500) -> str:
    """Keep the initial task plus recent informative constraints in order.

    Empty means insufficient user evidence; callers must not substitute a
    document prefix or assistant response to manufacture a retrieval hit.
    """
    meaningful = []
    for turn in user_turns:
        text = turn.strip()
        if text and not _CLOSING.fullmatch(text) and text not in meaningful:
            meaningful.append(text)
    if not meaningful or max_chars <= 0:
        return ""
    chosen = meaningful if len(meaningful) <= 3 else [meaningful[0], *meaningful[-2:]]
    # Split the budget so a long initial request cannot erase later constraints.
    budget = max(1, (max_chars - len(chosen) + 1) // len(chosen))
    return "\n".join(t[:budget] for t in chosen)[:max_chars]
