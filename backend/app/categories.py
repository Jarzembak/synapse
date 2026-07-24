"""Quick-ref category registry: built-in kinds + user-defined custom ones.

Built-ins (tool / technique / concept / technology) ship with doc prompts in
tasks/prompts.py (editable via Settings → Advanced → Prompt editor). Custom
categories live in the Settings table (key `quickref.custom_categories`) and
carry their own doc prompt plus a description that is appended to the
entity-extraction call so the LLM knows to classify into them.

A category's `dir` is the library folder its docs live in (tools/, concepts/,
…) and its `key` doubles as the QuickRef.kind value and the quickref_<key>
artifact type — both are fixed at creation so existing docs never orphan.
"""
from __future__ import annotations

from .settings_store import get_setting, set_settings_if_no_analysis_jobs

CUSTOM_KEY = "quickref.custom_categories"

BUILTINS: list[dict] = [
    {"key": "tool", "label": "Tool", "plural": "Tools",
     "icon": "🔧", "dir": "tools", "builtin": True},
    {"key": "technique", "label": "Technique", "plural": "Techniques",
     "icon": "🎯", "dir": "techniques", "builtin": True},
    {"key": "concept", "label": "Concept", "plural": "Concepts",
     "icon": "💡", "dir": "concepts", "builtin": True},
    {"key": "technology", "label": "Technology", "plural": "Technologies",
     "icon": "⚙️", "dir": "technologies", "builtin": True},
]
BUILTIN_KEYS = {c["key"] for c in BUILTINS}


def custom_categories() -> list[dict]:
    cats = get_setting(CUSTOM_KEY) or []
    return [{**c, "builtin": False} for c in cats]


def save_custom_categories(cats: list[dict]) -> None:
    set_settings_if_no_analysis_jobs({
        CUSTOM_KEY: [
            {k: v for k, v in c.items() if k != "builtin"} for c in cats
        ],
    })


def all_categories() -> list[dict]:
    return BUILTINS + custom_categories()


def category_map() -> dict[str, dict]:
    return {c["key"]: c for c in all_categories()}


def kind_dir(kind: str) -> str:
    cat = category_map().get(kind)
    return cat["dir"] if cat else f"{kind}s"
