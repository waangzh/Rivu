from app.services.export.naming import export_filename, sanitize_filename


def test_sanitize_filename_replaces_whitespace():
    assert sanitize_filename("my report.csv") == "my_report.csv"


def test_default_filename_has_format_suffix():
    name = export_filename("abcdef123456", "markdown")
    assert name.startswith("rivu-abcdef12-")
    assert name.endswith(".markdown")
