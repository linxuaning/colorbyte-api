import importlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_task_store_hydrates_completed_task_from_persistent_sqlite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "artimagehub.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("METRICS_DATABASE_URL", raising=False)

    import app.config as config
    import app.services.database as database
    import app.services.task_store as task_store

    config.get_settings.cache_clear()
    database._db_path = None
    importlib.reload(task_store)

    database.init_db()

    upload_path = tmp_path / "uploads" / "sample.jpg"
    result_path = tmp_path / "results" / "sample_result.jpg"
    upload_path.parent.mkdir(parents=True)
    result_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"upload-bytes")
    result_path.write_bytes(b"result-bytes")

    task = task_store.create_task(
        file_id="sample",
        upload_path=str(upload_path),
        email="paid@example.com",
    )
    task_store.update_task(
        task.id,
        status=task_store.TaskStatus.COMPLETED,
        progress=100,
        stage="Complete",
        result_path=str(result_path),
        provider_used="photofix:m2",
        provider_backend="m2_top1_proxy",
    )

    completed = task_store.get_task(task.id)
    database.upsert_persistent_task(
        task.id,
        json.dumps(task_store.asdict(completed)),
        upload_bytes=upload_path.read_bytes(),
        upload_content_type="image/jpeg",
        result_bytes=result_path.read_bytes(),
        result_content_type="image/jpeg",
    )

    (tmp_path / "tasks" / f"{task.id}.json").unlink()
    upload_path.unlink()
    result_path.unlink()
    task_store._tasks.clear()

    restored = task_store.get_task(task.id)

    assert restored is not None
    assert restored.status == task_store.TaskStatus.COMPLETED
    assert restored.provider_used == "photofix:m2"
    assert restored.provider_backend == "m2_top1_proxy"
    assert upload_path.read_bytes() == b"upload-bytes"
    assert result_path.read_bytes() == b"result-bytes"
