from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

openapi_tags = [
    {
        "name": "Health",
        "description": "Service health and diagnostics endpoints.",
    },
    {
        "name": "Tasks",
        "description": "CRUD operations for todo tasks.",
    },
]


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a trailing 'Z'."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_connection_txt_for_path(connection_txt: str) -> Optional[str]:
    """Parse db_connection.txt content and attempt to extract the file path."""
    # Example line:
    # # File path: /abs/path/to/myapp.db
    m = re.search(r"^\s*#\s*File path:\s*(.+?)\s*$", connection_txt, flags=re.MULTILINE)
    if m:
        return m.group(1).strip()

    # Example line:
    # # Connection string: sqlite:////abs/path/to/myapp.db
    m = re.search(
        r"^\s*#\s*Connection string:\s*(sqlite:(?P<slashes>/{2,4})(?P<path>.+?))\s*$",
        connection_txt,
        flags=re.MULTILINE,
    )
    if m:
        return m.group("path").strip()

    return None


# PUBLIC_INTERFACE
def resolve_sqlite_db_path() -> str:
    """Resolve the SQLite DB path used by the API.

    Resolution order:
    1) Environment variable SQLITE_DB (as provided by the database container).
    2) Parse ../database/db_connection.txt (the database container writes this file).
    3) Fall back to ../database/myapp.db (matches database/init_db.py fallback name).

    Returns:
        A filesystem path suitable for sqlite3.connect().
    """
    env_db = os.getenv("SQLITE_DB")
    if env_db:
        return env_db

    # Try to read the database container's db_connection.txt (repo sibling workspace).
    # backend container: simple-todo-list-.../todo_backend/src/api/main.py
    # db container:      simple-todo-list-.../database/db_connection.txt
    repo_root = Path(__file__).resolve().parents[3]  # .../todo_backend
    db_hint_path = (repo_root.parent / "simple-todo-list-309712-309721" / "database" / "db_connection.txt").resolve()
    if db_hint_path.exists():
        try:
            content = db_hint_path.read_text(encoding="utf-8", errors="ignore")
            parsed = _parse_db_connection_txt_for_path(content)
            if parsed:
                return parsed
        except Exception:
            # If hints cannot be read/parsed, fall through.
            pass

    # Final fallback: match the database container's default name/location.
    fallback = (repo_root.parent / "simple-todo-list-309712-309721" / "database" / "myapp.db").resolve()
    return str(fallback)


@contextmanager
def _get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager that yields a SQLite connection with row_factory configured."""
    db_path = resolve_sqlite_db_path()
    # Ensure parent directories exist if a nested path is used.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


def _ensure_schema() -> None:
    """Ensure the tasks table exists (idempotent safeguard).

    The database container init_db.py already creates this table, but the backend
    also ensures it exists so local/dev runs don't break.
    """
    with _get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '' NOT NULL,
                completed INTEGER DEFAULT 0 NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


class TaskBase(BaseModel):
    """Shared task fields."""

    title: str = Field(..., min_length=1, description="Short task title.")
    description: str = Field("", description="Optional longer task description.")
    completed: bool = Field(False, description="Whether the task is completed.")


class TaskCreate(BaseModel):
    """Request body for creating a task."""

    title: str = Field(..., min_length=1, description="Short task title.")
    description: str = Field("", description="Optional longer task description.")
    completed: bool = Field(False, description="Whether the task is completed (default false).")


class TaskUpdate(BaseModel):
    """Request body for updating a task. All fields are optional."""

    title: Optional[str] = Field(None, min_length=1, description="Updated task title.")
    description: Optional[str] = Field(None, description="Updated task description.")
    completed: Optional[bool] = Field(None, description="Updated completion state.")


class TaskOut(TaskBase):
    """Response model for a task."""

    id: int = Field(..., description="Task ID.")
    created_at: str = Field(..., description="Creation timestamp (UTC ISO-8601).")
    updated_at: str = Field(..., description="Last update timestamp (UTC ISO-8601).")


def _row_to_task_out(row: sqlite3.Row) -> TaskOut:
    """Convert a sqlite row to TaskOut."""
    return TaskOut(
        id=int(row["id"]),
        title=str(row["title"]),
        description=str(row["description"]),
        completed=bool(int(row["completed"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


app = FastAPI(
    title="Todo Backend API",
    description="FastAPI backend for a simple todo application (SQLite persistence).",
    version="0.1.0",
    openapi_tags=openapi_tags,
)

# CORS: allow React dev server and also permit other origins in dev if needed.
# Frontend is expected at http://localhost:3000, while backend runs on 3001.
allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "*",  # keep permissive for development; tighten for production deployments
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    """Ensure DB schema exists on startup."""
    _ensure_schema()


@app.get("/", tags=["Health"], summary="Health check", description="Simple health check endpoint.")
def health_check() -> Dict[str, str]:
    """Health check endpoint.

    Returns:
        JSON object indicating service is healthy.
    """
    return {"message": "Healthy"}


@app.get(
    "/api/tasks",
    response_model=List[TaskOut],
    tags=["Tasks"],
    summary="List all tasks",
    description="Return all tasks ordered by id descending (newest first).",
)
def list_tasks() -> List[TaskOut]:
    """List all tasks.

    Returns:
        A list of tasks.
    """
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, title, description, completed, created_at, updated_at
            FROM tasks
            ORDER BY id DESC
            """
        ).fetchall()
    return [_row_to_task_out(r) for r in rows]


@app.post(
    "/api/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Tasks"],
    summary="Create a task",
    description="Create a new task. Title is required. Description optional. Completed defaults to false.",
)
def create_task(payload: TaskCreate) -> TaskOut:
    """Create a new task.

    Args:
        payload: TaskCreate body.

    Returns:
        The created task with generated id and timestamps.
    """
    now = _utc_now_iso()
    with _get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (title, description, completed, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (payload.title, payload.description or "", int(bool(payload.completed)), now, now),
        )
        conn.commit()
        task_id = int(cur.lastrowid)

        row = conn.execute(
            """
            SELECT id, title, description, completed, created_at, updated_at
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()

    if row is None:
        # Extremely unlikely; indicates DB inconsistency.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create task.")

    return _row_to_task_out(row)


@app.put(
    "/api/tasks/{task_id}",
    response_model=TaskOut,
    tags=["Tasks"],
    summary="Update a task",
    description="Update one or more fields on a task (title, description, completed).",
)
def update_task(task_id: int, payload: TaskUpdate) -> TaskOut:
    """Update an existing task.

    Args:
        task_id: ID of the task to update.
        payload: TaskUpdate body (all fields optional).

    Returns:
        The updated task.

    Raises:
        HTTPException: 404 if task not found.
    """
    update_data: Dict[str, Any] = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields provided for update.",
        )

    with _get_db() as conn:
        existing = conn.execute(
            """
            SELECT id, title, description, completed, created_at, updated_at
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")

        fields = []
        params: List[Any] = []

        if "title" in update_data:
            fields.append("title = ?")
            params.append(update_data["title"])
        if "description" in update_data:
            fields.append("description = ?")
            params.append(update_data["description"] if update_data["description"] is not None else "")
        if "completed" in update_data:
            fields.append("completed = ?")
            params.append(int(bool(update_data["completed"])))

        fields.append("updated_at = ?")
        params.append(_utc_now_iso())

        params.append(task_id)

        conn.execute(
            f"""
            UPDATE tasks
            SET {", ".join(fields)}
            WHERE id = ?
            """,
            tuple(params),
        )
        conn.commit()

        row = conn.execute(
            """
            SELECT id, title, description, completed, created_at, updated_at
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")

    return _row_to_task_out(row)


@app.delete(
    "/api/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Tasks"],
    summary="Delete a task",
    description="Delete a task by id.",
)
def delete_task(task_id: int) -> Response:
    """Delete an existing task.

    Args:
        task_id: ID of the task to delete.

    Returns:
        204 No Content on success.

    Raises:
        HTTPException: 404 if task not found.
    """
    with _get_db() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()

    if cur.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")

    return Response(status_code=status.HTTP_204_NO_CONTENT)
