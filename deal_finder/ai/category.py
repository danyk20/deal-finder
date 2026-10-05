"""AI-picked marketplace category: map what a watch is looking for onto ONE category from
a marketplace's own category tree (as offered by its scraper package).

The tree is walked one level at a time -- pick a top-level category, then one of its
children, and so on -- instead of listing every category in one prompt: Ricardo alone has
~1700, far more than a local model's context comfortably holds. Options are numbered and
the model answers with numbers, so it can only ever pick a category the scraper offered.

Besides the best pick, the runner-ups are kept (ranked) so the user can switch to one of
them on the watch's edit form: at each level the model ranks its top few options, and the
tree is explored best-first in that order until enough candidates are collected.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from ..adapters.base import SiteCategory
from .client import AiUnavailable, OllamaClient

_SYSTEM = (
    "You pick the category of a Swiss second-hand marketplace that a buyer's search belongs "
    "in, so the marketplace search can be limited to it. You are shown one level of the "
    "marketplace's category tree at a time. Rank the options the searched item would "
    "actually be listed under -- the item itself, not accessories or spare parts for it, "
    "unless the buyer is clearly looking for those. Answer with exactly 3 different option "
    "numbers, best match first, then the next best alternatives, separated by commas (e.g. "
    "4, 2, 9), and nothing else."
)
# ("exactly 3": asked for "up to 3", gemma4 without reasoning only ever gave its single
# best answer, which left the edit form with no alternatives to offer.)

# Safety net against a malformed tree (a cycle) -- real trees are at most ~5 deep.
_MAX_DEPTH = 10
# Options ranked per level: enough to reach siblings AND neighbouring branches of the best
# pick within the top 5, without asking about every branch.
_PER_LEVEL = 3
# Sub-category names shown next to an option that has children. Without them a small model
# guesses from the bare name (live: "Mac Mini, desktop computer" -> "office business"
# instead of "computers accessories (computers, ..., software, tablets)").
_MAX_HINTS = 8


def _option_text(node: SiteCategory, children: dict[str | None, list[SiteCategory]]) -> str:
    kids = children.get(node.id) or []
    if not kids:
        return node.name
    hint = ", ".join(k.name for k in kids[:_MAX_HINTS]) + (", ..." if len(kids) > _MAX_HINTS else "")
    return f"{node.name} ({hint})"


def _ask(client: OllamaClient, prompt: str, n_options: int) -> list[int]:
    """The model's ranked option numbers (0..n_options, best first, at most _PER_LEVEL)."""
    # Plain numbers, not json_mode: with gemma4, Ollama's JSON mode returned empty answers
    # or ran into the timeout. No reasoning: a pick is a lookup, not worth ~100s per level.
    raw = client.chat(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=20,
        reasoning_effort="none",
    )
    ranked: list[int] = []
    for token in re.findall(r"-?\d+", raw or ""):
        n = int(token)
        if 0 <= n <= n_options and n not in ranked:
            ranked.append(n)
    if not ranked:
        # Not cached by the caller (see site_categories.py): a garbled answer is treated
        # like an unreachable model, so the next run simply asks again.
        raise AiUnavailable(f"unusable category answer from the model: {raw!r}")
    return ranked[:_PER_LEVEL]


def rank_site_categories(
    client: OllamaClient, item_text: str, site_label: str, nodes: Iterable[SiteCategory], *, limit: int = 5
) -> list[SiteCategory | None]:
    """Up to ``limit`` categories, best first. ``None`` stands for "no category fits"
    (-> search all categories) and only ever appears once. The first entry is the AI's
    pick. Raises AiUnavailable if not even the pick could be made; if the model fails
    while looking for runner-ups, what was found so far is returned."""
    nodes = list(nodes)
    children: dict[str | None, list[SiteCategory]] = {}
    for n in nodes:
        children.setdefault(n.parent_id, []).append(n)

    def walk(current: SiteCategory | None, path: list[str]) -> Iterator[SiteCategory | None]:
        options = children.get(current.id if current else None, [])
        if not options or len(path) >= _MAX_DEPTH:
            yield current  # a leaf: nothing narrower to choose from
            return
        if current is not None and current.selectable:
            zero = f"Stay at '{' > '.join(path)}': no narrower option below clearly fits better"
        else:
            zero = "None of these fits the item: search all categories instead"
        lines = [f"0. {zero}"] + [f"{i}. {_option_text(o, children)}" for i, o in enumerate(options, start=1)]
        prompt = (
            f"WHAT THE BUYER IS LOOKING FOR:\n{item_text}\n\n"
            f"MARKETPLACE: {site_label}\n"
            f"CURRENT CATEGORY: {' > '.join(path) if path else '(top level)'}\n"
            "OPTIONS:\n" + "\n".join(lines)
        )
        for choice in _ask(client, prompt, len(options)):
            if choice == 0:
                if current is None:
                    yield None  # nothing fits at all
                elif current.selectable:
                    yield current  # stay at this level
                # a non-selectable group (e.g. a tutti group): this branch yields nothing
            else:
                node = options[choice - 1]
                yield from walk(node, [*path, node.name])

    found: list[SiteCategory | None] = []
    seen: set[str | None] = set()
    candidates = walk(None, [])
    while len(found) < limit:
        try:
            node = next(candidates)
        except StopIteration:
            break
        except AiUnavailable:
            if not found:
                raise
            break  # keep the pick and whatever runner-ups were already found
        if node is not None and not node.selectable:
            continue
        key = node.id if node is not None else None
        if key not in seen:
            seen.add(key)
            found.append(node)
    return found
