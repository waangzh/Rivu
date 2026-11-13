from app.services.review.snapshot_reader import SnapshotReader


def test_read_file_inside_root(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("print('ok')\n", encoding="utf-8")
    reader = SnapshotReader(tmp_path)
    assert "print" in reader.read("app/a.py")


def test_search_file_inside_root(tmp_path):
    (tmp_path / "app.py").write_text("needle\n", encoding="utf-8")
    reader = SnapshotReader(tmp_path)
    hits = reader.search("needle")
    assert hits and hits[0]["line"] == 1
    assert hits[0]["path"].endswith("app.py")
