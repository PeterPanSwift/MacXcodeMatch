#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrape Apple Developer Xcode Releases table and output clean JSON.

Output schema (UTF-8):
[
  {
    "xcode_version": "15.0",
    "macos_version": "13.5+",
    "sdks": {
      "iOS": "17.0",
      "iPadOS": "17.0",
      "macOS": "14.0",
      "tvOS": "17.0",
      "watchOS": "10.0",
      "visionOS": "1.0"
    }
  },
  ...
]

Usage:
  python xcode_releases_scraper.py [--url https://developer.apple.com/support/xcode] [-o xcode_releases.json]
"""

import argparse
import json
import re
import sys
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup, Tag


XCODE_URL_DEFAULT = "https://developer.apple.com/support/xcode"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def fetch_html(url: str) -> str:
    """Fetch HTML content from URL."""
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_xcode_tables(soup: BeautifulSoup) -> List[Tag]:
    """
    Find all Xcode Releases tables.
    The page has multiple tables: 'Latest Xcode versions' and 'Other Xcode versions'.
    """
    tables = []
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if any("xcode" in h for h in headers):
            tables.append(table)
    
    if not tables:
        raise RuntimeError("Could not locate any Xcode Releases tables.")
    
    return tables


def clean_version_text(text: str) -> str:
    """Clean up version text, removing extra whitespace and special characters."""
    # Remove HTML entities (with or without semicolon) and non-breaking spaces
    text = text.replace('&nbsp;', ' ').replace('&nbsp', ' ')
    text = text.replace('\xa0', ' ').replace('\u00a0', ' ')
    # Normalize various dash characters to standard hyphen
    text = text.replace('–', '-').replace('—', '-').replace('\u2013', '-').replace('\u2014', '-')
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def parse_macos_versions(text: str) -> str:
    """
    Parse macOS version requirements and return as a string (preserving ranges).
    Examples:
      "macOS Sonoma 14.0 or later" -> "14.0+"
      "macOS Ventura 13.5" -> "13.5"
      "macOS Sonoma 14.5 - macOS Sequoia 15.x" -> "14.5 - 15.x"
      "macOS Sequoia 15.2 - macOS Sequoia 15.x" -> "15.2 - 15.x"
      "macOS Sonoma 14.x" -> "14.x"
      "macOS Ventura 13.x" -> "13.x"
    """
    text = clean_version_text(text)
    
    # Check for range pattern with enhanced regex to handle all cases
    # Matches: "15.2 - macOS Sequoia 15.x", "14.5 - 15.x", "15.2 - 15.x"
    range_match = re.search(r'(\d+\.(?:\d+|x)(?:\.\d+)?)\s*[-–]\s*(?:macOS\s+\w+\s+)?(\d+\.(?:\d+|x)(?:\.\d+)?)', text)
    if range_match:
        start_ver = range_match.group(1)
        end_ver = range_match.group(2)
        return f"{start_ver} - {end_ver}"
    
    # Check for "or later" pattern
    if "or later" in text.lower():
        versions = re.findall(r'\b(\d+\.\d+(?:\.\d+)?)\b', text)
        if versions:
            return f"{versions[0]}+"
    
    # Find version numbers including .x format (e.g., "14.x", "13.x")
    version_match = re.search(r'\b(\d+\.(?:\d+|x)(?:\.\d+)?)\b', text)
    if version_match:
        return version_match.group(1)
    
    # Fallback: find regular version numbers
    versions = re.findall(r'\b(\d+\.\d+(?:\.\d+)?)\b', text)
    return versions[0] if versions else ""


def parse_sdk_column(text: str) -> Dict[str, str]:
    """
    Parse SDK information.
    Examples:
      "iOS 17.0, iPadOS 17.0, macOS 14.0, tvOS 17.0, watchOS 10.0, visionOS 1.0"
      "iOS 16.4, iPadOS 16.4, macOS 13.3, tvOS 16.4, watchOS 9.4"
      "iOS 16.1 tvOS 16.1 watchOS 9.1 macOS 13 DriverKit 22.1"
      "iOS 16 tvOS 16 watchOS 9 macOS 12.3 DriverKit 22"
    
    Returns a dict like:
      {"iOS": "17.0", "iPadOS": "17.0", "macOS": "14.0", ...}
    """
    text = clean_version_text(text)
    sdks = {}
    
    # Pattern: platform name followed by version number (with or without decimal point)
    # Matches: "iOS 17.0", "macOS 14", "macOS 13.3", etc.
    pattern = r'(iOS|iPadOS|macOS|tvOS|watchOS|visionOS|DriverKit)\s+(\d+(?:\.\d+)?(?:\.\d+)?)'
    matches = re.findall(pattern, text, re.IGNORECASE)
    
    for platform, version in matches:
        # Normalize platform name
        platform_key = platform.strip()
        sdks[platform_key] = version
    
    return sdks


def parse_table(table: Tag) -> List[Dict[str, object]]:
    """Extract rows from the Xcode table."""
    # Find header indices
    headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
    
    idx_xcode: Optional[int] = None
    idx_macos: Optional[int] = None
    idx_sdk: Optional[int] = None
    
    for i, h in enumerate(headers):
        if "xcode" in h:
            idx_xcode = i
        elif "macos" in h or "minimum" in h or "os" in h:
            idx_macos = i
        elif "sdk" in h:
            idx_sdk = i
    
    if idx_xcode is None:
        raise RuntimeError(f"Could not find Xcode column in headers: {headers}")
    
    results = []
    
    for tr in table.find_all("tr")[1:]:  # Skip header row
        cells = tr.find_all(["td", "th"])
        if len(cells) <= max(filter(lambda x: x is not None, [idx_xcode, idx_macos, idx_sdk])):
            continue
        
        # Extract Xcode version
        xcode_text = clean_version_text(cells[idx_xcode].get_text(" ", strip=True))
        # Extract version number (e.g., "15.0" from "Xcode 15.0" or "15.0.1" or "16" from "Xcode 16")
        # Match formats: "16.4.1", "16.4", or just "16"
        xcode_match = re.search(r'\b(\d+(?:\.\d+)?(?:\.\d+)?)\b', xcode_text)
        if not xcode_match:
            continue
        xcode_version = xcode_match.group(1)
        
        # Extract macOS versions
        macos_version = ""
        if idx_macos is not None and idx_macos < len(cells):
            macos_text = cells[idx_macos].get_text(" ", strip=True)
            macos_version = parse_macos_versions(macos_text)
        
        # Extract SDKs
        sdks = {}
        if idx_sdk is not None and idx_sdk < len(cells):
            sdk_text = cells[idx_sdk].get_text(" ", strip=True)
            sdks = parse_sdk_column(sdk_text)
        
        results.append({
            "xcode_version": xcode_version,
            "macos_version": macos_version,
            "sdks": sdks
        })
    
    return results


def main():
    ap = argparse.ArgumentParser(description="Scrape Xcode Releases table from Apple Developer site")
    ap.add_argument("--url", default=XCODE_URL_DEFAULT, help="Apple Xcode support page URL")
    ap.add_argument("-o", "--out", default="xcode_releases.json", help="Output JSON path")
    args = ap.parse_args()

    print(f"Fetching {args.url}...")
    html = fetch_html(args.url)
    
    print("Parsing HTML...")
    soup = BeautifulSoup(html, "html.parser")
    tables = find_xcode_tables(soup)
    
    print(f"Found {len(tables)} Xcode tables")
    print("Extracting data...")
    
    # Parse all tables and combine results
    all_data = []
    for i, table in enumerate(tables, 1):
        print(f"  Processing table {i}...")
        rows = parse_table(table)
        all_data.extend(rows)
    
    data = all_data
    
    print(f"Total: {len(data)} Xcode releases")
    
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    # Show first and last entries as examples
    if data:
        print("\nFirst entry:")
        print(json.dumps(data[0], ensure_ascii=False, indent=2))
        if len(data) > 1:
            print("\nLast entry:")
            print(json.dumps(data[-1], ensure_ascii=False, indent=2))
    
    print(f"\n✓ Saved to {args.out}")


if __name__ == "__main__":
    main()
