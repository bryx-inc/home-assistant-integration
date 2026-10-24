"""Diagnose why Home Assistant will not load the Bryx 911 config flow.

Run this with the same Python that runs Home Assistant, from your config
directory (the one holding custom_components/):

    python3 custom_components/bryx911/../../scripts/diagnose.py

or point it at the config directory explicitly:

    python3 scripts/diagnose.py /config
"""

import json
from pathlib import Path
import sys
import traceback

DOMAIN = "bryx911"

# Import order matters: report the first module that actually fails, not the
# one that merely imports the broken one.
MODULES = [
    "const",
    "models",
    "queries",
    "api",
    "subscription",
    "config_flow",
    "coordinator",
    "entity",
    "sensor",
    "binary_sensor",
    "switch",
    "calendar",
    "diagnostics",
    "__init__",
]

REQUIRED_FILES = ["manifest.json", "__init__.py", "config_flow.py", "const.py"]


def main() -> int:
    """Report the first thing that stops the integration loading."""
    config_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    integration = config_dir / "custom_components" / DOMAIN

    print(f"python            : {sys.version.split()[0]}")
    try:
        from homeassistant.const import __version__ as ha_version

        print(f"home assistant    : {ha_version}")
    except Exception as err:
        print(f"home assistant    : NOT IMPORTABLE ({err})")
        print("\nRun this with the Python that runs Home Assistant.")
        return 2

    print(f"config directory  : {config_dir}")
    print(f"integration path  : {integration}")

    if not integration.is_dir():
        print(f"\nFAIL: {integration} does not exist.")
        print("The integration must live at <config>/custom_components/bryx911/.")
        return 1

    print("\n-- files --")
    missing = [name for name in REQUIRED_FILES if not (integration / name).is_file()]
    for name in REQUIRED_FILES:
        print(f"  {'ok  ' if (integration / name).is_file() else 'MISS'} {name}")
    if missing:
        print(f"\nFAIL: missing {', '.join(missing)}.")
        return 1

    stale = sorted(p.name for p in integration.glob("*.py"))
    print(f"\n-- python modules present ({len(stale)}) --")
    print("  " + ", ".join(stale))
    expected = {f"{m}.py" for m in MODULES}
    unexpected = sorted(set(stale) - expected)
    absent = sorted(expected - set(stale))
    if unexpected:
        print(f"\n  leftovers from an older version : {unexpected}")
    if absent:
        print(f"\n  MISSING from this version       : {absent}")
    if unexpected or absent:
        print(
            "\n  FAIL: this looks like new files copied over an old install.\n"
            "  Delete the whole bryx911 directory, copy the new one in fresh,\n"
            "  then restart Home Assistant."
        )
        return 1

    print("\n-- manifest --")
    try:
        manifest = json.loads((integration / "manifest.json").read_text())
    except json.JSONDecodeError as err:
        print(f"FAIL: manifest.json is not valid JSON: {err}")
        return 1
    print(f"  domain      : {manifest.get('domain')}")
    print(f"  version     : {manifest.get('version')}")
    print(f"  config_flow : {manifest.get('config_flow')}")
    if manifest.get("domain") != DOMAIN:
        print(f"\nFAIL: manifest domain must be '{DOMAIN}'.")
        return 1
    if manifest.get("config_flow") is not True:
        print('\nFAIL: manifest must contain "config_flow": true.')
        return 1

    print("\n-- imports --")
    sys.path.insert(0, str(config_dir))
    failed = False
    for module in MODULES:
        name = f"custom_components.{DOMAIN}" + (
            "" if module == "__init__" else f".{module}"
        )
        try:
            __import__(name)
        except Exception:
            print(f"  FAIL {name}")
            print()
            traceback.print_exc()
            print()
            failed = True
            break
        print(f"  ok   {name}")

    if failed:
        print("The traceback above is what Home Assistant hides behind")
        print('"Invalid handler specified". Send it over.')
        return 1

    print("\nEverything imports. If the UI still says 'Invalid handler specified',")
    print("restart Home Assistant fully so it rescans custom_components.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
