from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

import requests

import model_settings as settings
from botaloid import LyricBot, ModelError
from botaloid.chat import ChatInput

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DEFAULT_DATABASE = ROOT / "botaloid.sqlite3"
if not DEFAULT_DATABASE.exists() and (ROOT / "vocaloid_rag.sqlite3").exists():
    DEFAULT_DATABASE = ROOT / "vocaloid_rag.sqlite3"
DATABASE = Path(settings.environment_value("DB", DEFAULT_DATABASE))
ADMIN = settings.environment_value("ADMIN", "1") == "1"
bot = LyricBot(DATABASE)


@asynccontextmanager
async def lifespan(_app):
    bot.initialize()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Lyric = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1800)]


class LineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Lyric
    translation: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1800)] | None = None
    stanza: int = Field(default=0, ge=0, le=3000)
    fallback: bool = False


class SongInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Name
    author: Name
    enabled: bool = True
    lyrics: list[LineInput | Lyric] = Field(min_length=1, max_length=3000)


class EnabledInput(BaseModel):
    enabled: bool


@app.get("/")
def chat_page():
    return FileResponse(WEB / "index.html")


@app.get("/api/config")
def config(request: Request):
    return {"admin": ADMIN and local_request(request)}


@app.post("/api/chat")
def send(payload: ChatInput):
    loaded, original_languages, translation_languages = settings.active_runtime()
    connection = None

    def model(*args):
        nonlocal connection
        if connection is None:
            connection = settings.connection_for(loaded)
        return settings.request_model(connection, *args)

    try:
        return bot.reply(
            payload.message, payload.history, payload.tone, model=model,
            original_languages=original_languages,
            translation_languages=translation_languages,
        )
    except ModelError as error:
        raise HTTPException(503, str(error)) from error


def local_request(request):
    return (request.client is not None and request.client.host in {"127.0.0.1", "::1"}
            and request.url.hostname in {"127.0.0.1", "::1", "localhost"})


def require_local(request: Request):
    if not local_request(request):
        raise HTTPException(404, "页面不存在。")
    if request.method in {"POST", "PUT", "PATCH"}:
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "请求来源无效。")


admin = APIRouter(dependencies=[Depends(require_local)])


@admin.get("/admin")
def admin_page():
    return RedirectResponse("/settings")


@admin.get("/settings")
def settings_page():
    return FileResponse(WEB / "admin.html")


@admin.get("/admin/model")
def legacy_model_page():
    return RedirectResponse("/settings/model")


@admin.get("/settings/model")
def model_page():
    return FileResponse(WEB / "model.html")


@admin.get("/settings/appearance")
def appearance_page():
    return FileResponse(WEB / "appearance.html")


@app.exception_handler(settings.SettingsError)
async def settings_error(_request, error):
    return JSONResponse({"detail": str(error)}, status_code=error.status)


@admin.get("/api/model-settings")
def model_settings():
    return JSONResponse(settings.public_settings(settings.load_settings()),
                        headers={"Cache-Control": "no-store"})


@admin.put("/api/model-settings")
def save_model_settings(payload: settings.Draft):
    return settings.save_settings(payload)


@admin.post("/api/model-settings/test")
def test_model_settings(payload: settings.Draft):
    connection = settings.draft_connection(payload)
    try:
        settings.test_model(connection)
    except ModelError as error:
        raise HTTPException(502, str(error)) from error
    return {"ok": True}


@admin.get("/api/model-settings/models")
def available_models(provider: settings.Provider, url: Annotated[str, Query(max_length=2048)]):
    target = settings.models_url(provider, url)
    headers = settings.models_headers(provider, url)
    try:
        response = requests.get(target, headers=headers, timeout=(4, 6), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError("unexpected model list response")
        entries = response.json()["models" if provider == "ollama" else "data"]
        field = "name" if provider == "ollama" else "id"
        models = sorted({item[field] for item in entries if isinstance(item.get(field), str)})
    except (requests.RequestException, KeyError, ValueError, TypeError, AttributeError) as error:
        raise HTTPException(502, "无法读取模型列表，请检查地址和运行状态。") from error
    return {"models": models}


@admin.get("/api/songs")
def songs(q: Annotated[str, Query(max_length=300)] = "",
          status: Literal["all", "enabled", "disabled"] = "all",
          page: Annotated[int, Query(ge=1, le=100000)] = 1):
    clauses, params = [], []
    if q.strip():
        clauses.append("(instr(lower(title),lower(?))>0 OR instr(lower(author),lower(?))>0)")
        params.extend([q.strip(), q.strip()])
    if status != "all":
        clauses.append("enabled=?")
        params.append(status == "enabled")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(bot.connect()) as db:
        total = db.execute("SELECT count(*) FROM songs" + where, params).fetchone()[0]
        items = db.execute("SELECT id,title,author,enabled FROM songs" + where
                           + " ORDER BY id LIMIT 25 OFFSET ?", [*params, (page - 1) * 25]).fetchall()
    return {"items": [dict(row) for row in items], "total": total}


def require_song(db, song_id):
    row = db.execute("SELECT id,title,author,enabled FROM songs WHERE id=?", (song_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "找不到这首歌。")
    return dict(row)


@admin.get("/api/songs/{song_id}")
def song(song_id: int):
    with closing(bot.connect()) as db:
        result = require_song(db, song_id)
        lyrics = db.execute(
            "SELECT stanza,text,translation,fallback_kind IS NOT NULL AS fallback "
            "FROM lyric_lines WHERE song_id=? ORDER BY position", (song_id,)
        )
        return {**result, "lyrics": [dict(line) for line in lyrics]}


def write_song(db, payload, song_id=None):
    lines = [LineInput(text=line) if isinstance(line, str) else line for line in payload.lyrics]
    if any(a.stanza > b.stanza for a, b in zip(lines, lines[1:])):
        raise HTTPException(422, "歌词段落顺序无效。")
    metadata = (payload.title, payload.author, payload.enabled)
    if song_id is None:
        song_id = db.execute("INSERT INTO songs(title,author,enabled) VALUES(?,?,?)", metadata).lastrowid
    else:
        require_song(db, song_id)
        db.execute("UPDATE songs SET title=?,author=?,enabled=? WHERE id=?", (*metadata, song_id))
    incoming = [(line.stanza, line.text, line.translation or None,
                 "default" if line.fallback else None) for line in lines]
    existing = [tuple(row) for row in db.execute(
        "SELECT stanza,text,translation,fallback_kind FROM lyric_lines WHERE song_id=? ORDER BY position",
        (song_id,)
    )]
    if existing != incoming:
        db.execute("DELETE FROM lyric_lines WHERE song_id=?", (song_id,))
        db.executemany(
            "INSERT INTO lyric_lines(song_id,position,stanza,text,translation,fallback_kind) VALUES(?,?,?,?,?,?)",
            ((song_id, pos, *line) for pos, line in enumerate(incoming, 1)),
        )
    return song_id, existing != incoming


@admin.post("/api/songs", status_code=201)
def create_song(payload: SongInput):
    with bot.lock, closing(bot.connect()) as db:
        with db:
            song_id, _ = write_song(db, payload)
        bot.invalidate()
    return {"id": song_id}


@admin.put("/api/songs/{song_id}")
def update_song(song_id: int, payload: SongInput):
    with bot.lock, closing(bot.connect()) as db:
        with db:
            _, changed = write_song(db, payload, song_id)
        if changed:
            bot.invalidate()
    return {"id": song_id}


@admin.patch("/api/songs/{song_id}")
def set_enabled(song_id: int, payload: EnabledInput):
    with bot.lock, closing(bot.connect()) as db, db:
        require_song(db, song_id)
        db.execute("UPDATE songs SET enabled=? WHERE id=?", (payload.enabled, song_id))
    return {"id": song_id, "enabled": payload.enabled}


@admin.post("/api/import", status_code=201)
def import_songs(payload: Annotated[list[SongInput], Field(min_length=1, max_length=10000)]):
    with bot.lock, closing(bot.connect()) as db:
        with db:
            for item in payload:
                write_song(db, item)
        bot.invalidate()
    return {"count": len(payload)}


if ADMIN:
    app.include_router(admin)
