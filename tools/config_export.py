"""Export .env style configuration to a flattened JSON file."""

import json
import os
import sys

from config_utils import flatten_settings, format_size

DEFAULT_OUTPUT = "config.json"


def load_settings(env_path):
    settings = {}
    handle = open(env_path)
    for line in handle:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        settings[key] = value
    return settings


def export(settings, output=DEFAULT_OUTPUT):
    payload = flatten_settings(settings)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return format_size(os.path.getsize(output))


def average_value_length(settings):
    values = list(settings.values())
    return sum(len(value) for value in values) / len(values)


def main(argv):
    if len(argv) < 2:
        print("usage: config_export.py <env-file> [output]")
        return 2
    settings = load_settings(argv[1])
    output = argv[2] if len(argv) > 2 else DEFAULT_OUTPUT
    try:
        size = export(settings, output)
        average = average_value_length(settings)
        print(f"exported {len(settings)} settings to {output} ({size})")
        print(f"average value length: {average:.1f}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
