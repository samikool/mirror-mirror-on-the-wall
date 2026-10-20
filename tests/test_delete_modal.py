"""DeleteProgressModal's delete: every file goes, then every emptied directory,
deepest first, with the count it shows."""
from mmw.tui.modals import DeleteProgressModal


def test_delete_files_removes_files_and_directories(tmp_path):
    root = tmp_path / "victim"
    for rel in ("a/b/c/f1", "a/b/f2", "a/f3", "f4"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")
    modal = DeleteProgressModal("Delete", [root])
    assert modal._count_files() == 4
    modal._delete_files()
    assert modal._deleted == 4
    assert not root.exists()
