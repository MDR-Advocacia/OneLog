"""Validate the browser cookies that can be delivered to OneLog clients."""

import json


BLOCKED_COOKIE_NAMES = {"PD-S-SESSION-ID", "JSESSIONID", "cf_clearance", "__cf_bm"}


def usable_pool_cookies(cookies):
    if not isinstance(cookies, list):
        return []

    usable = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        if not isinstance(name, str) or not name or name in BLOCKED_COOKIE_NAMES:
            continue
        if name.startswith("TS01") or "BIGipServer" in name:
            continue
        if not cookie.get("value") or not cookie.get("domain"):
            continue
        usable.append(cookie)
    return usable


def load_pool_cookies(payload):
    if not payload:
        return []
    try:
        return usable_pool_cookies(json.loads(payload))
    except (TypeError, ValueError):
        return []
