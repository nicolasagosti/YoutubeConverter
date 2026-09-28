# YouTube → Texto

Convierte cualquier video de YouTube en texto a partir de su URL.

1. **Subtítulos de YouTube**: si el video tiene subtítulos (manuales o automáticos),
   se usan directamente. Es instantáneo y no descarga el video.
2. **Whisper**: si no hay subtítulos, descarga solo el audio y lo transcribe en tu
   computadora con [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
   Es más lento, pero funciona con cualquier video que tenga voz.
   *Solo disponible ejecutando la app localmente (no en Vercel).*

El resultado se puede ver como texto en párrafos o con marcas de tiempo, copiar,
o descargar como `.txt` o `.srt`.

## Estructura

| Archivo | Qué es |
| --- | --- |
| `transcriber.py` | Lógica principal. También funciona como herramienta de terminal |
| `app.py` | Servidor web (Flask) |
| `public/` | Interfaz web (HTML, CSS, JS) |
| `requirements.txt` | Dependencias mínimas: solo subtítulos (las que usa Vercel) |
| `requirements-whisper.txt` | Instalación completa, con Whisper |

## Instalación local

Requiere Python 3.10 o superior. No hace falta ffmpeg.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-whisper.txt
```

En Debian/Kali sin el paquete `python3-venv` (error de "ensurepip"):

```bash
python3 -m venv --without-pip .venv
pip --python .venv/bin/python install pip
.venv/bin/pip install -r requirements-whisper.txt
```

## Uso: aplicación web

```bash
.venv/bin/python app.py
```

Abrí <http://127.0.0.1:5000>, pegá la URL y presioná **Convertir**.
Se puede cambiar el puerto con `PORT=8080 .venv/bin/python app.py`.

## Uso: terminal

```bash
.venv/bin/python transcriber.py "https://youtu.be/VIDEO_ID"
.venv/bin/python transcriber.py "https://youtu.be/VIDEO_ID" -l es -o transcripcion.txt
.venv/bin/python transcriber.py "https://youtu.be/VIDEO_ID" -m whisper --model small -f srt -o subs.srt
```

| Opción | Valores | Descripción |
| --- | --- | --- |
| `-m`, `--method` | `auto` (por defecto), `subtitles`, `whisper` | De dónde sale el texto |
| `-l`, `--lang` | `es`, `en`, `pt`, … | Idioma deseado (por defecto, el original) |
| `--model` | `tiny`, `base` (por defecto), `small`, `medium`, `large-v3` | Modelo de Whisper: más grande = más preciso y más lento |
| `-f`, `--format` | `txt` (por defecto), `timestamps`, `srt` | Formato de salida |
| `-o`, `--output` | archivo | Guardar en un archivo en vez de mostrarlo |

## Publicar en Vercel

La versión online usa **solo subtítulos**: Whisper pesa demasiado para Vercel
(unos 470 MB de dependencias, 1 CPU y 5 minutos máximo por petición en el plan gratuito).
La interfaz lo detecta y oculta sus opciones.

1. Subí este repositorio a GitHub.
2. En [vercel.com/new](https://vercel.com/new), importá el repositorio.
   Vercel detecta Flask solo (`app.py` con la variable `app`); no hace falta configurar nada.
3. Presioná **Deploy**.

### Si aparece "YouTube bloqueó la descarga de subtítulos…"

YouTube bloquea la mayoría de las IPs de servidores en la nube, y Vercel corre sobre AWS.
Es muy probable que sin un proxy la versión online no pueda leer subtítulos.
La solución es un proxy residencial: en Vercel, andá a **Settings → Environment Variables**,
agregá una de estas opciones y volvé a desplegar:

| Variable | Uso |
| --- | --- |
| `WEBSHARE_PROXY_USERNAME` y `WEBSHARE_PROXY_PASSWORD` | Proxy residencial rotativo de [Webshare](https://www.webshare.io/) (el que recomienda youtube-transcript-api) |
| `PROXY_URL` | Cualquier otro proxy, por ejemplo `http://usuario:clave@host:puerto` |

Aun con proxy, YouTube puede bloquearlo. Ejecutada en tu computadora, la app no tiene este problema.

## Notas

- La primera vez que usás un modelo de Whisper se descarga (de ~75 MB para `tiny` a ~3 GB
  para `large-v3`) y queda guardado en `~/.cache/huggingface`.
- Sin GPU, Whisper `base` tarda aproximadamente entre un cuarto y la mitad de la duración del video;
  `small` y `medium` son bastante más lentos. Con una GPU NVIDIA (CUDA) se usa automáticamente.
- Si pedís un idioma que el video no tiene, se intenta la traducción automática de YouTube.
  YouTube a veces la bloquea: en ese caso se usan los subtítulos originales y se muestra un aviso.
  Whisper no traduce a otros idiomas: transcribe lo que se habla en el video.
- yt-dlp recomienda tener instalado [Deno](https://deno.com) para descargar de YouTube de forma
  más confiable. Hoy funciona sin él, pero si la descarga de audio empieza a fallar, instalalo
  y actualizá yt-dlp: `.venv/bin/pip install -U yt-dlp`.
