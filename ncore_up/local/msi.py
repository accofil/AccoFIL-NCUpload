"""Windows Installer (.msi) packages, read statically with msitools (``msiinfo``); nothing is installed or run.

``read(path)`` returns

    {"product_name": "7-Zip 26.04 (x64 edition)", "name": "7-Zip", "manufacturer": "Igor Pavlov", "version": "26.04.00.0",
     "product_code": "{...}", "upgrade_code": "{...}", "url": ARPURLINFOABOUT, "help_url": ARPHELPLINK,
     "update_url": ARPURLUPDATEINFO, "arch": "x64" | "x86" | "arm64", "lcids": [1033], "languages": ["angol"],
     "author", "subject", "creating_app": "Windows Installer XML ...", "min_os": "Windows 7" (LaunchCondition), "properties": {...}}

{} when msiinfo is missing or the file is not an MSI.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .. import safeio

from .pe import lcid_name

TEMPLATE_ARCH = {"intel": "x86", "x64": "x64", "amd64": "x64", "intel64": "ia64", "arm64": "arm64", "arm": "arm"}
# VersionNT values in LaunchCondition: 501 XP, 600 Vista, 601 7, 602 8, 603 8.1, 1000 10.
VERSIONNT = {501: "Windows XP", 502: "Windows XP x64", 600: "Windows Vista", 601: "Windows 7", 602: "Windows 8", 603: "Windows 8.1", 1000: "Windows 10"}


def _run(*args: str, timeout: int = 60) -> str:
    if not shutil.which("msiinfo"):
        return ""
    try:
        run = safeio.run(["msiinfo", *args], timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return run.stdout.decode("utf-8", errors="replace") if run.returncode == 0 else ""


def parse_table(text: str) -> list[list[str]]:
    """Rows of an ``msiinfo export`` (IDT) table: the three header lines are skipped."""

    lines = [line.rstrip("\r") for line in (text or "").split("\n")]
    return [line.split("\t") for line in lines[3:] if line.strip()]


def parse_property_table(text: str) -> dict:
    return {row[0]: row[1] for row in parse_table(text) if len(row) >= 2}


def parse_suminfo(text: str) -> dict:
    """``msiinfo suminfo`` lines -> {"title", "subject", "author", "template", "arch", "lcids", "creating_app", ...}"""

    out: dict = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[re.sub(r"\W+", "_", key.strip().lower()).strip("_")] = value.strip()
    platform, _, langs = out.get("template", "").partition(";")
    out["arch"] = TEMPLATE_ARCH.get(platform.strip().lower(), "x86" if not platform.strip() and "template" in out else "")
    out["lcids"] = [int(x) for x in re.findall(r"\d+", langs)]
    out["creating_app"] = out.pop("application", "")
    return out


def clean_name(product_name: str, version: str = "") -> str:
    """'7-Zip 26.04 (x64 edition)' -> '7-Zip' (version and architecture notes removed)."""

    name = re.sub(r"\s*[(\[][^)\]]*(?:x64|x86|64-bit|32-bit|edition|arm64)[^)\]]*[)\]]", "", product_name or "", flags=re.I)
    numbers = re.findall(r"\d+", version or "")
    if numbers:
        name = re.sub(r"\s+v?" + re.escape(numbers[0]) + r"(?:[.,]\d+)*\s*$", "", name.strip())
    name = re.sub(r"\s+(?:x64|x86|64-bit|32-bit)$", "", name, flags=re.I)
    return name.strip(" -") or (product_name or "").strip()


def min_os(conditions: list[list[str]]) -> str:
    """Lowest Windows a LaunchCondition table requires ('VersionNT >= 601' -> 'Windows 7')."""

    best = 0
    for row in conditions:
        for match in re.finditer(r"VersionNT(?:64)?\s*>=?\s*(\d{3,4})", row[0] if row else ""):
            best = max(best, int(match.group(1)))
    return next((name for value, name in sorted(VERSIONNT.items(), reverse=True) if best >= value), "") if best else ""


def summarize(properties: dict, summary: dict, conditions: list[list[str]] | None = None) -> dict:
    version = properties.get("ProductVersion", "")
    lcids = summary.get("lcids") or ([int(properties["ProductLanguage"])] if properties.get("ProductLanguage", "").isdigit() else [])
    return {
        "product_name": properties.get("ProductName", ""),
        "name": clean_name(properties.get("ProductName", ""), version),
        "manufacturer": properties.get("Manufacturer", "") or summary.get("author", ""),
        "version": version,
        "product_code": properties.get("ProductCode", ""),
        "upgrade_code": properties.get("UpgradeCode", ""),
        "url": properties.get("ARPURLINFOABOUT", ""),
        "help_url": properties.get("ARPHELPLINK", ""),
        "update_url": properties.get("ARPURLUPDATEINFO", ""),
        "arch": summary.get("arch", ""),
        "lcids": lcids,
        "languages": list(dict.fromkeys(n for n in (lcid_name(x) for x in lcids) if n)),
        "author": summary.get("author", ""),
        "subject": summary.get("subject", ""),
        "creating_app": summary.get("creating_app", ""),
        "min_os": min_os(conditions or []),
        "properties": properties,
    }


def read(path: str | Path) -> dict:
    path = str(path)
    properties = parse_property_table(_run("export", path, "Property"))
    if not properties:
        return {}
    summary = parse_suminfo(_run("suminfo", path))
    tables = _run("tables", path).split()
    conditions = parse_table(_run("export", path, "LaunchCondition")) if "LaunchCondition" in tables else []
    return summarize(properties, summary, conditions)
