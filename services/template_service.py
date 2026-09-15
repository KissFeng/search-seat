#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_CACHE = {}


def get_template(name: str, use_cache: bool = True) -> str:
    if use_cache and name in _CACHE:
        return _CACHE[name]
    path = TEMPLATES_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {name}")
    content = path.read_text(encoding="utf-8")
    if use_cache:
        _CACHE[name] = content
    return content


def clear_template_cache():
    _CACHE.clear()
