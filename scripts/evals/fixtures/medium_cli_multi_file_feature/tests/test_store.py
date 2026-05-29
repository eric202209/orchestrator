from medium_cli.store import TaskStore


def test_store_tracks_completed_tasks():
    store = TaskStore()
    store.add("write docs", completed=True)
    store.add("ship feature", completed=False)

    assert [task.title for task in store.completed()] == ["write docs"]
