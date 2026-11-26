"""Category paths ("Sepatu > Sneakers"): find or create every level, the way an import names a category."""
import re

from django.utils.text import slugify

from .models import Category

SPLIT = re.compile(r"\s*[>›]\s*")


def split_path(text):
    """"Sepatu > Sneakers" → ["Sepatu", "Sneakers"] (at most ``Category.MAX_DEPTH`` levels, extra ones joined)."""
    parts = [part.strip()[:100] for part in SPLIT.split(text or "") if part.strip()]
    if len(parts) > Category.MAX_DEPTH:
        parts = parts[:Category.MAX_DEPTH - 1] + [" ".join(parts[Category.MAX_DEPTH - 1:])[:100]]
    return parts


def ensure_paths(store, texts):
    """``{text: Category}`` for every category path in ``texts``, creating the missing levels. A single name that
    matches an existing category anywhere (case-insensitive) uses it, so "Sneakers" alone finds Sepatu › Sneakers."""
    existing = list(Category.objects.filter(store=store).select_related("parent__parent"))
    by_path = {tuple(node.name.lower() for node in c.lineage): c for c in existing}
    by_name = {}
    for category in existing:
        by_name.setdefault(category.name.lower(), category)
    slugs = {c.slug for c in existing}
    found = {}
    for text in dict.fromkeys(t for t in texts if (t or "").strip()):
        parts = split_path(text)
        if not parts:
            continue
        if len(parts) == 1 and parts[0].lower() in by_name:
            found[text] = by_name[parts[0].lower()]
            continue
        parent = None
        for depth in range(1, len(parts) + 1):
            key = tuple(part.lower() for part in parts[:depth])
            node = by_path.get(key)
            if node is None:
                base = slugify(" ".join(parts[:depth]))[:45] or "kategori"
                slug, n = base, 2
                while slug in slugs:
                    slug, n = f"{base}-{n}", n + 1
                slugs.add(slug)
                node = Category.objects.create(store=store, name=parts[depth - 1], slug=slug, parent=parent)
                by_path[key] = node
                by_name.setdefault(node.name.lower(), node)
            parent = node
        found[text] = parent
    return found
