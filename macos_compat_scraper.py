#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape Wikipedia macOS 'Hardware compatibility' table and output clean JSON.

Output schema (UTF-8):
[
  {
    "os": "26",
    "supported_systems": [
      "MacBook Air (M1 or later)",
      "MacBook Pro (2019 or later)",
      "Mac Mini (M1 or later)",
      "iMac (2020 or later)",
      "Mac Studio (2022 or later)",
      "Mac Pro (2019 or later)"
    ]
  },
  ...
]

Usage:
  python macos_compat_scraper.py [--url https://en.wikipedia.org/wiki/MacOS] [-o hardware_compat.json]
"""

import argparse
import json
import re
import sys
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup, Tag


WIKI_URL_DEFAULT = "https://en.wikipedia.org/wiki/MacOS"
UA = "Mozilla/5.0 (compatible; macOS-compat-scraper/1.0; +https://example.local)"


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_hardware_table(soup: BeautifulSoup) -> Tag:
    """
    Prefer the section whose id is 'Hardware_compatibility', then pick the first following wikitable.
    Fallback: find a wikitable that has headers 'Operating system' and 'Supported systems'.
    """
    # 1) Try by id
    hardware_section = soup.find(id="Hardware_compatibility")
    if hardware_section:
        # walk siblings to find the first wikitable
        node = hardware_section.parent
        for sib in node.next_siblings:
            if isinstance(sib, Tag) and sib.name == "table" and "wikitable" in sib.get("class", []):
                return sib

    # 2) Fallback by header names
    for t in soup.find_all("table", class_="wikitable"):
        headers = [th.get_text(" ", strip=True) for th in t.find_all("th")]
        heads_lower = [h.lower() for h in headers]
        if any(h.startswith("operating system") for h in heads_lower) and any(
            h.startswith("supported systems") for h in heads_lower
        ):
            return t

    raise RuntimeError("Could not locate the 'Hardware compatibility' wikitable.")


def parse_table(table: Tag) -> List[Dict[str, object]]:
    """Extract raw rows: {'os': <str>, 'supported_systems': [<str>, ...]} (still unclean)."""
    # Map header -> index
    headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    idx_os: Optional[int] = None
    idx_sup: Optional[int] = None
    for i, h in enumerate(headers):
        hl = h.lower()
        if hl.startswith("operating system"):
            idx_os = i
        if hl.startswith("supported systems"):
            idx_sup = i

    if idx_os is None or idx_sup is None:
        raise RuntimeError(f"Unexpected table headers: {headers}")

    results = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) <= max(idx_os, idx_sup):
            continue
        os_text = " ".join(cells[idx_os].get_text(" ", strip=True).split())
        sup_text = cells[idx_sup].get_text(" ", strip=True)
        # Split first by commas to get coarse chunks; keep as list for later cleanup
        raw_items = [p.strip().strip(",") for p in sup_text.split(",") if p.strip()]
        results.append({"os": os_text, "supported_systems": raw_items})

    return results


LABEL_PAT = re.compile(r"\b(Laptops?|Desktops?)\s*:\s*", flags=re.IGNORECASE)


def split_category_labels(text: str) -> List[str]:
    """
    Split on 'Laptops :' / 'Desktops :' markers but keep BOTH sides as model fragments.
    E.g. "MacBook Pro (2019 or later) Desktops : Mac Mini (M1 or later)"
         -> ["MacBook Pro (2019 or later)", "Mac Mini (M1 or later)"]
    """
    parts: List[str] = []
    s = text.strip()
    while True:
        m = LABEL_PAT.search(s)
        if not m:
            break
        left = s[:m.start()].strip(" ,")
        if left:
            parts.append(left)
        s = s[m.end():].strip()
    if s:
        parts.append(s.strip(" ,"))
    return parts


def clean_supported_systems(raw_items: List[str]) -> List[str]:
    """
    1) Break by category labels (Laptops/Desktops).
    2) Further split by ',' if multiple models remain on one chunk.
    3) Trim, filter junk tokens (like 'and'), keep order, dedup.
    """
    out: List[str] = []
    for item in raw_items:
        chunks = split_category_labels(item)
        for ch in chunks:
            for sub in [c.strip() for c in ch.split(",") if c.strip()]:
                if sub.lower() in {"and"}:
                    continue
                # Normalize double spaces
                sub = re.sub(r"\s{2,}", " ", sub).strip(" ,")
                if sub:
                    out.append(sub)

    # De-duplicate while preserving order
    seen = set()
    uniq: List[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def normalize_os(os_text: str) -> str:
    """
    Extract version number(s) from the OS text.
    Handles single versions (e.g., '10.0', '13') and ranges (e.g., '10.0 – 10.2').
    """
    s = os_text.strip()
    # If it's a version number or range (e.g., "10.0", "10.0 – 10.2", "10.8 – 10.11"), use as-is
    if re.fullmatch(r"\d+(?:\.\d+)*(?:\s*[–—-]\s*\d+(?:\.\d+)*)?", s):
        return s
    # Otherwise, try to extract version number(s) from text like "macOS 10.0 Cheetah" or "macOS 10.8 – 10.11"
    # First try to match a range
    m = re.search(r"\b(\d+(?:\.\d+)*\s*[–—-]\s*\d+(?:\.\d+)*)\b", s)
    if m:
        return m.group(1)
    # If no range, try to find a single version number
    m = re.search(r"\b(\d+(?:\.\d+)*)\b", s)
    return m.group(1) if m else s


def should_include_os(os_str: str) -> bool:
    """
    Filter to include only OS versions >= 10.8.
    Handles both single versions and ranges.
    """
    # Extract the first version number from the string (e.g., "10.8" from "10.8 – 10.11")
    m = re.search(r"(\d+)(?:\.(\d+))?", os_str)
    if not m:
        return False
    
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else 0
    
    # Include if major version >= 11, or if major == 10 and minor >= 8
    return major >= 11 or (major == 10 and minor >= 8)


def build_clean_json(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    cleaned = []
    for r in rows:
        os_text = str(r.get("os", "")).strip()
        os_norm = normalize_os(os_text)
        
        # Filter: only include OS >= 10.8
        if not should_include_os(os_norm):
            continue
            
        systems_raw = r.get("supported_systems", []) or []
        systems_clean = clean_supported_systems([str(x) for x in systems_raw])
        cleaned.append({"os": os_norm, "supported_systems": systems_clean})
    return cleaned


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=WIKI_URL_DEFAULT, help="Wikipedia macOS URL")
    ap.add_argument("-o", "--out", default="hardware_compatibility_os_supported.json",
                    help="Output JSON path")
    args = ap.parse_args()

    html = fetch_html(args.url)
    soup = BeautifulSoup(html, "html.parser")
    table = find_hardware_table(soup)
    raw_rows = parse_table(table)
    data = build_clean_json(raw_rows)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Tiny sanity ping: try to show the line for OS '26' if any
    maybe_26 = next((x for x in data if x.get("os") == "26"), None)
    if maybe_26:
        print("OS 26 example:\n", json.dumps(maybe_26, ensure_ascii=False, indent=2))
    print(f"\nSaved -> {args.out}")


if __name__ == "__main__":
    main()
