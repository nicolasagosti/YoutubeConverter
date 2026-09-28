"""Convierte videos de YouTube en texto.

Estrategia:
1. Usar los subtítulos del video (manuales o automáticos) con youtube-transcript-api.
   Es casi instantáneo y no descarga nada.
2. Si no hay subtítulos, descargar solo el audio con yt-dlp y transcribirlo
   localmente con Whisper (faster-whisper).

También se puede usar desde la terminal:
    python transcriber.py "https://youtu.be/VIDEO_ID" -l es -o salida.txt
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    InvalidVideoId,
    IpBlocked,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

METHODS = ("auto", "subtitles", "whisper")
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_MODEL = "base"

WHISPER_UNAVAILABLE = (
    "La transcripción del audio con Whisper no está disponible en este servidor: "
    "solo funciona ejecutando la app en tu computadora."
)


@functools.cache
def whisper_available() -> bool:
    """Whisper es opcional (requirements-whisper.txt); en Vercel no se instala."""
    return all(importlib.util.find_spec(m) for m in ("faster_whisper", "yt_dlp"))


def _proxy_config():
    """Proxy opcional: YouTube bloquea la mayoría de las IPs de servidores en la nube."""
    user = os.environ.get("WEBSHARE_PROXY_USERNAME")
    password = os.environ.get("WEBSHARE_PROXY_PASSWORD")
    if user and password:
        return WebshareProxyConfig(proxy_username=user, proxy_password=password)
    if url := os.environ.get("PROXY_URL"):
        return GenericProxyConfig(http_url=url, https_url=url)
    return None

# Recibe un mensaje de estado y el avance (0..1), o None si no se conoce.
ProgressFn = Callable[[str, Optional[float]], None]


class TranscriptionError(Exception):
    """Error cuyo mensaje se puede mostrar tal cual al usuario."""

    code: str | None = None  # permite a la interfaz ofrecer ayuda específica


class SubtitlesUnavailable(TranscriptionError):
    """El video no tiene subtítulos utilizables (en modo auto se recurre a Whisper)."""


class YouTubeBlocked(SubtitlesUnavailable):
    """YouTube rechazó la consulta por venir de la IP de un servidor en la nube."""

    code = "blocked"


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Result:
    video_id: str
    title: str | None
    author: str | None
    source: str  # "subtitles" o "whisper"
    source_label: str
    language: str | None
    segments: list[Segment]
    notice: str | None = None

    @property
    def text(self) -> str:
        return build_paragraphs(self.segments)

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "url": f"https://www.youtube.com/watch?v={self.video_id}",
            "thumbnail": f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg",
            "title": self.title,
            "author": self.author,
            "source": self.source,
            "source_label": self.source_label,
            "language": self.language,
            "notice": self.notice,
            "duration": self.segments[-1].end if self.segments else 0,
            "text": self.text,
            "timestamped": to_timestamped(self.segments),
            "srt": to_srt(self.segments),
        }


# --------------------------------------------------------------------------- URL

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_PREFIXES = {"shorts", "embed", "live", "v", "e"}


def extract_video_id(url: str) -> str:
    """Obtiene el ID del video desde cualquier formato habitual de URL de YouTube."""
    url = url.strip()
    if _VIDEO_ID_RE.match(url):
        return url
    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    candidate = ""
    if host == "youtu.be":
        candidate = parts[0] if parts else ""
    elif host in ("youtube.com", "youtube-nocookie.com") or host.endswith(
        (".youtube.com", ".youtube-nocookie.com")
    ):
        if parts in ([], ["watch"]):
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
            candidate = parts[1]

    if _VIDEO_ID_RE.match(candidate):
        return candidate
    raise ValueError("No parece una URL válida de un video de YouTube.")


def fetch_metadata(video_id: str) -> tuple[str | None, str | None]:
    """Título y canal del video mediante oEmbed (no requiere API key)."""
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            return data.get("title"), data.get("author_name")
    except (requests.RequestException, ValueError):
        pass
    return None, None


# ------------------------------------------------------------------ Formato texto


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_paragraphs(segments: list[Segment], target: int = 500) -> str:
    """Une los fragmentos en párrafos: corta al final de una frase, en pausas largas
    o cuando el párrafo se hace demasiado largo (los subtítulos automáticos no tienen puntuación)."""
    paragraphs: list[str] = []
    current: list[str] = []
    length = 0
    for i, seg in enumerate(segments):
        current.append(seg.text)
        length += len(seg.text) + 1
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        long_pause = nxt is not None and nxt.start - seg.end > 2.0
        sentence_end = seg.text.endswith((".", "?", "!", "…"))
        if (
            nxt is None
            or (length >= target and sentence_end)
            or (long_pause and length >= 150)
            or length >= target * 2
        ):
            paragraphs.append(" ".join(current))
            current, length = [], 0
    return "\n\n".join(paragraphs)


def format_timestamp(seconds: float, srt: bool = False) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    if srt:
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def to_timestamped(segments: list[Segment]) -> str:
    return "\n".join(f"[{format_timestamp(s.start)}] {s.text}" for s in segments)


def to_srt(segments: list[Segment]) -> str:
    blocks = [
        f"{i}\n{format_timestamp(s.start, True)} --> {format_timestamp(max(s.end, s.start), True)}\n{s.text}\n"
        for i, s in enumerate(segments, 1)
    ]
    return "\n".join(blocks)


# -------------------------------------------------------------------- Subtítulos


def _same_language(a: str, b: str) -> bool:
    """'es', 'es-419' y 'es-ES' cuentan como el mismo idioma."""
    return a.lower().split("-")[0] == b.lower().split("-")[0]


def _subtitles_error(exc: CouldNotRetrieveTranscript) -> TranscriptionError:
    if isinstance(exc, (VideoUnavailable, InvalidVideoId)):
        return TranscriptionError("El video no existe o no está disponible.")
    if isinstance(exc, TranscriptsDisabled):
        return SubtitlesUnavailable("El video no tiene subtítulos.")
    if isinstance(exc, (IpBlocked, RequestBlocked)):
        return YouTubeBlocked(
            "YouTube bloqueó la consulta que hizo el servidor de esta página. "
            "No es un problema del video ni de tu conexión."
        )
    return SubtitlesUnavailable("No se pudieron obtener los subtítulos del video.")


def fetch_subtitles(
    video_id: str, language: str | None = None
) -> tuple[list[Segment], str, str, str | None]:
    """Devuelve (segmentos, código de idioma, descripción de la fuente, aviso)."""
    try:
        transcript_list = YouTubeTranscriptApi(proxy_config=_proxy_config()).list(video_id)
        # Los subtítulos manuales suelen ser más fieles que los automáticos.
        available = sorted(transcript_list, key=lambda t: t.is_generated)
        if not available:
            raise SubtitlesUnavailable("El video no tiene subtítulos.")

        chosen, translate_to, notice = available[0], None, None
        if language:
            same = [t for t in available if _same_language(t.language_code, language)]
            if same:
                chosen = same[0]
            else:
                for t in available:
                    code = next(
                        (tl.language_code for tl in t.translation_languages
                         if _same_language(tl.language_code, language)),
                        None,
                    )
                    if t.is_translatable and code:
                        chosen, translate_to = t, code
                        break
                else:
                    notice = (f"El video no tiene subtítulos en el idioma pedido; "
                              f"se usaron los de {chosen.language}.")

        try:
            fetched = (chosen.translate(translate_to) if translate_to else chosen).fetch()
        except CouldNotRetrieveTranscript:
            if not translate_to:
                raise
            fetched, translate_to = chosen.fetch(), None
            notice = f"YouTube no permitió traducir los subtítulos; se usaron los de {chosen.language}."
    except CouldNotRetrieveTranscript as exc:
        raise _subtitles_error(exc) from exc

    segments = [
        Segment(s.start, s.start + s.duration, _clean(s.text))
        for s in fetched.snippets
        if _clean(s.text)
    ]
    if not segments:
        raise SubtitlesUnavailable("Los subtítulos del video están vacíos.")

    if translate_to:
        label = f"Subtítulos traducidos por YouTube · {fetched.language}"
    else:
        kind = "automáticos" if chosen.is_generated else "manuales"
        label = f"Subtítulos {kind} · {fetched.language}"
    return segments, fetched.language_code, label, notice


# ----------------------------------------------------------------------- Whisper

_models: dict = {}
_models_lock = threading.Lock()
# Whisper usa toda la CPU: las transcripciones se hacen de a una.
_whisper_lock = threading.Lock()


def _load_model(name: str):
    with _models_lock:
        if name not in _models:
            import ctranslate2
            from faster_whisper import WhisperModel

            if ctranslate2.get_cuda_device_count() > 0:
                _models[name] = WhisperModel(name, device="cuda", compute_type="float16")
            else:
                _models[name] = WhisperModel(
                    name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4
                )
        return _models[name]


def _download_audio(video_id: str, dest_dir: str, report: ProgressFn) -> tuple[str, dict]:
    import yt_dlp

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            report("Descargando audio…", d["downloaded_bytes"] / total if total else None)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    if proxy := os.environ.get("PROXY_URL"):
        opts["proxy"] = proxy
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            return ydl.prepare_filename(info), info
    except yt_dlp.utils.DownloadError as exc:
        reason = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).removeprefix("ERROR: ")
        raise TranscriptionError(f"No se pudo descargar el audio: {reason}") from exc


def transcribe_audio(
    video_id: str, language: str | None, model_name: str, report: ProgressFn
) -> tuple[list[Segment], str | None, str | None]:
    """Devuelve (segmentos, idioma detectado, título según yt-dlp)."""
    with tempfile.TemporaryDirectory(prefix="ytconv-") as tmp:
        report("Descargando audio…", None)
        audio_path, info = _download_audio(video_id, tmp, report)

        if not _whisper_lock.acquire(blocking=False):
            report("En cola: hay otra transcripción en curso…", None)
            _whisper_lock.acquire()
        try:
            report(f"Cargando modelo Whisper «{model_name}» (la primera vez se descarga)…", None)
            model = _load_model(model_name)
            report("Transcribiendo audio…", 0.0)
            lang = language.split("-")[0].lower() if language else None
            try:
                seg_iter, whisper_info = model.transcribe(audio_path, language=lang, vad_filter=True)
                segments = []
                for s in seg_iter:
                    if text := _clean(s.text):
                        segments.append(Segment(s.start, s.end, text))
                    if whisper_info.duration:
                        report("Transcribiendo audio…", min(s.end / whisper_info.duration, 1.0))
            except ValueError as exc:
                raise TranscriptionError(f"Whisper no reconoce el idioma «{language}».") from exc
        finally:
            _whisper_lock.release()

    if not segments:
        raise TranscriptionError("No se detectó voz en el audio del video.")
    return segments, whisper_info.language, info.get("title")


# -------------------------------------------------------------------- Principal


def transcribe(
    url: str,
    method: str = "auto",
    language: str | None = None,
    model: str = DEFAULT_MODEL,
    progress: ProgressFn | None = None,
) -> Result:
    """Convierte el video en texto.

    method: "auto" (subtítulos y, si no hay, Whisper), "subtitles" o "whisper".
    language: código de idioma deseado (p. ej. "es"); None para usar el original.
    """
    if method not in METHODS:
        raise ValueError(f"Método desconocido: {method}")
    if model not in WHISPER_MODELS:
        raise ValueError(f"Modelo desconocido: {model}")
    report = progress or (lambda message, fraction: None)
    if method == "whisper" and not whisper_available():
        raise TranscriptionError(WHISPER_UNAVAILABLE)

    try:
        video_id = extract_video_id(url)
    except ValueError as exc:
        raise TranscriptionError(str(exc)) from exc

    report("Obteniendo información del video…", None)
    title, author = fetch_metadata(video_id)

    notice = None
    if method != "whisper":
        report("Buscando subtítulos…", None)
        try:
            segments, lang, label, notice = fetch_subtitles(video_id, language)
            return Result(video_id, title, author, "subtitles", label, lang, segments, notice)
        except SubtitlesUnavailable as exc:
            if method == "subtitles":
                raise
            if not whisper_available():
                # Se conserva el tipo de error para no perder su código.
                raise type(exc)(f"{exc} {WHISPER_UNAVAILABLE}") from exc
            notice = f"{exc} Se transcribió el audio con Whisper."

    segments, lang, yt_title = transcribe_audio(video_id, language, model, report)
    return Result(
        video_id, title or yt_title, author, "whisper",
        f"Transcripción con Whisper · modelo {model}", lang, segments, notice,
    )


def _print_progress(message: str, fraction: float | None) -> None:
    line = message if fraction is None else f"{message} {fraction:4.0%}"
    print(f"\r\033[K{line}", end="", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Convierte un video de YouTube en texto.")
    parser.add_argument("url", help="URL o ID del video")
    parser.add_argument("-m", "--method", choices=METHODS, default="auto",
                        help="auto: subtítulos y, si no hay, Whisper (por defecto)")
    parser.add_argument("-l", "--lang", help="idioma deseado, p. ej. es, en, pt")
    parser.add_argument("--model", choices=WHISPER_MODELS, default=DEFAULT_MODEL,
                        help=f"modelo de Whisper (por defecto {DEFAULT_MODEL})")
    parser.add_argument("-f", "--format", choices=("txt", "timestamps", "srt"), default="txt",
                        help="formato de salida")
    parser.add_argument("-o", "--output", help="archivo de salida (por defecto, la pantalla)")
    args = parser.parse_args()

    try:
        result = transcribe(args.url, args.method, args.lang, args.model, _print_progress)
    except TranscriptionError as exc:
        print(f"\r\033[KError: {exc}", file=sys.stderr)
        return 1

    output = {
        "txt": result.text,
        "timestamps": to_timestamped(result.segments),
        "srt": to_srt(result.segments),
    }[args.format]
    print(f"\r\033[K{result.title or result.video_id} — {result.source_label}", file=sys.stderr)
    if result.notice:
        print(f"Aviso: {result.notice}", file=sys.stderr)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output + "\n")
        print(f"Guardado en {args.output}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
