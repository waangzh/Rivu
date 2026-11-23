"""Helpers for flattening and formatting configuration values."""


def flatten_settings(settings, prefix="", result={}):
    for key, value in settings.items():
        result[f"{prefix}{key}"] = value
    return result


def format_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.0f} TB"
