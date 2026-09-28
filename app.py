"""Servidor web de YouTube → Texto.

Uso:
    .venv/bin/python app.py
y abrir http://127.0.0.1:5000

En Vercel se detecta solo (variable `app`). Ahí no se instala Whisper, así que cada
conversión se resuelve dentro de la misma petición, sin tareas en segundo plano.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

from flask import Flask, jsonify, request

import transcriber

# Vercel sirve public/ desde su CDN; en local lo sirve Flask.
app = Flask(__name__, static_folder="public", static_url_path="")

JOB_TTL = 60 * 60  # los resultados se guardan en memoria durante una hora
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/config")
def config():
    return jsonify(whisper=transcriber.whisper_available())


@app.post("/api/transcribe")
def start_job():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    method = data.get("method") or "auto"
    language = (data.get("language") or "").strip() or None
    model = data.get("model") or transcriber.DEFAULT_MODEL

    if method not in transcriber.METHODS:
        return jsonify(error="Método no válido."), 400
    if model not in transcriber.WHISPER_MODELS:
        return jsonify(error="Modelo de Whisper no válido."), 400
    try:
        transcriber.extract_video_id(url)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    # Sin Whisper solo se leen subtítulos (unos segundos): se responde directamente.
    # Así funciona en servidores sin estado como Vercel, donde las tareas en memoria
    # no sobreviven entre peticiones.
    if not transcriber.whisper_available():
        try:
            result = transcriber.transcribe(url, method, language, model)
        except transcriber.TranscriptionError as exc:
            return jsonify(error=str(exc), code=exc.code), 422
        return jsonify(result=result.to_dict())

    job = {
        "id": uuid.uuid4().hex,
        "status": "running",
        "message": "Iniciando…",
        "progress": None,
        "result": None,
        "error": None,
        "code": None,
        "created": time.time(),
    }
    with jobs_lock:
        for old_id in [k for k, j in jobs.items() if time.time() - j["created"] > JOB_TTL]:
            del jobs[old_id]
        jobs[job["id"]] = job

    threading.Thread(
        target=_run_job, args=(job, url, method, language, model), daemon=True
    ).start()
    return jsonify(id=job["id"]), 202


def _run_job(job: dict, url: str, method: str, language: str | None, model: str) -> None:
    def progress(message: str, fraction: float | None) -> None:
        job["message"], job["progress"] = message, fraction

    try:
        job["result"] = transcriber.transcribe(url, method, language, model, progress).to_dict()
        job["status"] = "done"
    except transcriber.TranscriptionError as exc:
        job["error"], job["code"], job["status"] = str(exc), exc.code, "error"
    except Exception as exc:  # noqa: BLE001 - cualquier fallo debe llegar a la interfaz
        app.logger.exception("Error inesperado al transcribir %s", url)
        job["error"], job["status"] = f"Error inesperado: {exc}", "error"


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return jsonify(error="La tarea no existe o ya expiró."), 404
    return jsonify({k: job[k] for k in ("status", "message", "progress", "result", "error", "code")})


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"YouTube → Texto disponible en http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
