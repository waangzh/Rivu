from app.services.export.storage import ExportStorage


def test_storage_round_trip(tmp_path):
    storage = ExportStorage(tmp_path)
    storage.write("task-1", "report.md", "# 报告\n")
    assert storage.read("task-1", "report.md") == "# 报告\n"
    assert (tmp_path / "task-1" / "report.md").exists()
