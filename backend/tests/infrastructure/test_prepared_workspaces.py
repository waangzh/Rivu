from pathlib import Path
from types import SimpleNamespace

from app.services.workspaces.prepared_workspaces import PreparedWorkspaces

PR = SimpleNamespace(base=SimpleNamespace(sha="base"), head=SimpleNamespace(sha="head"))


def _source(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspaces"
    source = root / "PennyLaneAI-pennylane-1-abc"
    (source / ".git").mkdir(parents=True)
    return root, source


def test_publish_retries_a_transient_windows_lock(persistence, tmp_path, monkeypatch) -> None:
    store, _, _ = persistence
    root, source = _source(tmp_path)
    original, calls = Path.rename, []

    def flaky(self, target):
        calls.append(self)
        if len(calls) == 1:
            raise PermissionError("locked")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", flaky)

    assert PreparedWorkspaces(store, root).publish("key", source, PR, {}) is True
    assert len(calls) == 2
    assert not source.exists()
    assert any(path.name.startswith(".prepared-") for path in root.iterdir())


def test_publish_skips_cache_when_the_lock_persists(persistence, tmp_path, monkeypatch) -> None:
    store, _, _ = persistence
    root, source = _source(tmp_path)

    def locked(self, target):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "rename", locked)

    assert PreparedWorkspaces(store, root).publish("key", source, PR, {}) is False
    assert source.is_dir()
