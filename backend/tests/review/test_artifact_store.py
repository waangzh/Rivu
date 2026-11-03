import pytest

from app.services.review.artifact_store import LocalArtifactStore


@pytest.fixture
def artifact_store(tmp_path):
    return LocalArtifactStore(tmp_path / "artifacts")


@pytest.mark.parametrize("directory_name", ["artifacts", "artifact space", "归档 #100%"])
@pytest.mark.parametrize("content", ["", "evidence\n", "line\r\n源码\r\n"])
def test_text_round_trip_preserves_content(tmp_path, monkeypatch, directory_name, content):
    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore(tmp_path / directory_name)
    artifact = store.put_text(task_id="review", kind="evidence", content=content)

    assert store.read_text(artifact.uri) == content


@pytest.mark.parametrize("working_directory", ["artifacts", "elsewhere"])
def test_artifact_read_is_independent_of_working_directory(
    artifact_store, tmp_path, monkeypatch, working_directory
):
    artifact = artifact_store.put_text(task_id="review", kind="evidence", content="fixed source")
    directory = tmp_path / working_directory
    directory.mkdir(exist_ok=True)
    monkeypatch.chdir(directory)

    assert artifact_store.read_text(artifact.uri) == "fixed source"


@pytest.mark.parametrize("uri", [
    "https://example.test/artifact.txt",
    "file://remote-host/artifact.txt",
    "file:/artifact.txt",
    "file:artifact.txt",
    "../artifact.txt",
])
def test_unsupported_artifact_uri_is_rejected(artifact_store, uri):
    with pytest.raises(ValueError, match="unsupported artifact URI"):
        artifact_store.read_text(uri)


def test_artifact_outside_root_is_rejected(artifact_store, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content", encoding="utf-8")

    with pytest.raises(ValueError, match="artifact path escapes configured root"):
        artifact_store.read_text(outside.as_uri())


def test_encoded_path_traversal_is_rejected(artifact_store, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content", encoding="utf-8")
    uri = artifact_store.root.as_uri() + "/%2e%2e/outside.txt"

    with pytest.raises(ValueError, match="artifact path escapes configured root"):
        artifact_store.read_text(uri)


def test_artifact_symlink_outside_root_is_rejected(artifact_store, tmp_path):
    artifact_store.root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content", encoding="utf-8")
    link = artifact_store.root / "linked.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(ValueError, match="artifact path escapes configured root"):
        artifact_store.read_text(link.as_uri())


def test_missing_artifact_is_not_reported_as_path_escape(artifact_store):
    missing = artifact_store.root / "missing.txt"

    with pytest.raises(FileNotFoundError):
        artifact_store.read_text(missing.as_uri())
