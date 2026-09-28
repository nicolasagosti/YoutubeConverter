const $ = (id) => document.getElementById(id);

let result = null;
let view = "text";
let whisperEnabled = true;

// En Vercel no hay Whisper: se ocultan sus opciones y solo se usan subtítulos.
fetch("/api/config")
  .then((res) => res.json())
  .then((config) => {
    whisperEnabled = config.whisper;
    if (whisperEnabled) return;
    $("method").value = "subtitles";
    $("method-field").hidden = true;
    $("model-field").hidden = true;
    $("hint").textContent =
      "Esta versión online usa los subtítulos de YouTube. Para transcribir videos sin " +
      "subtítulos (con Whisper), ejecutá la app en tu computadora. Si elegís un idioma " +
      "que el video no tiene, se intenta la traducción automática de YouTube.";
  })
  .catch(() => {});

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = $("url").value.trim();
  if (!url) {
    $("url").focus();
    return;
  }

  setBusy(true);
  $("error").hidden = true;
  $("result").hidden = true;
  showStatus(whisperEnabled ? "Iniciando…" : "Buscando subtítulos…", null);

  try {
    const res = await fetch("/api/transcribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        method: $("method").value,
        language: $("language").value,
        model: $("model").value,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "No se pudo iniciar la conversión.");
    // Sin Whisper el servidor responde el resultado directamente; si no, devuelve una tarea.
    if (data.result) showResult(data.result);
    else poll(data.id);
  } catch (err) {
    fail(err);
  }
});

async function poll(jobId) {
  try {
    const res = await fetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    if (!res.ok) throw new Error(job.error);

    if (job.status === "done") {
      showResult(job.result);
    } else if (job.status === "error") {
      throw new Error(job.error);
    } else {
      showStatus(job.message, job.progress);
      setTimeout(() => poll(jobId), 800);
    }
  } catch (err) {
    fail(err);
  }
}

function setBusy(busy) {
  $("submit").disabled = busy;
  $("submit").textContent = busy ? "Convirtiendo…" : "Convertir";
}

function showStatus(message, progress) {
  $("status").hidden = false;
  $("status-msg").textContent = message;
  const known = typeof progress === "number";
  $("status-pct").textContent = known ? `${Math.round(progress * 100)} %` : "";
  $("bar-fill").style.width = known ? `${progress * 100}%` : "";
  $("status").querySelector(".bar").classList.toggle("indeterminate", !known);
}

function fail(err) {
  setBusy(false);
  $("status").hidden = true;
  $("error").textContent =
    err instanceof TypeError ? "No se pudo conectar con el servidor. ¿Sigue corriendo app.py?" : err.message;
  $("error").hidden = false;
}

function showResult(data) {
  result = data;
  setBusy(false);
  $("status").hidden = true;

  $("thumb").src = data.thumbnail;
  $("thumb-link").href = data.url;
  $("title").href = data.url;
  $("title").textContent = data.title || data.video_id;
  $("author").textContent = data.author || "";
  $("source").textContent = data.source_label;

  const words = data.text.split(/\s+/).filter(Boolean).length;
  $("stats").textContent = `${words.toLocaleString("es")} palabras · ${formatDuration(data.duration)}`;

  $("notice").textContent = data.notice || "";
  $("notice").hidden = !data.notice;

  setView("text");
  $("result").hidden = false;
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

function setView(name) {
  view = name;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === name);
    tab.setAttribute("aria-selected", tab.dataset.view === name);
  });
  $("output").textContent = result[name];
  $("output").classList.toggle("mono", name === "timestamped");
  $("output").scrollTop = 0;
}

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => setView(tab.dataset.view))
);

$("copy").addEventListener("click", async () => {
  const button = $("copy");
  try {
    await navigator.clipboard.writeText(result[view]);
    button.textContent = "¡Copiado!";
  } catch {
    // Sin permiso de portapapeles: se selecciona el texto para copiarlo a mano.
    const range = document.createRange();
    range.selectNodeContents($("output"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    button.textContent = "Seleccionado: Ctrl+C";
  }
  setTimeout(() => (button.textContent = "Copiar"), 1800);
});

$("dl-txt").addEventListener("click", () => download(result[view], "txt", "text/plain"));
$("dl-srt").addEventListener("click", () => download(result.srt, "srt", "application/x-subrip"));

function download(content, ext, type) {
  const name = (result.title || result.video_id).replace(/[\\/:*?"<>|]+/g, "").trim().slice(0, 100);
  const blob = new Blob([content + "\n"], { type: `${type};charset=utf-8` });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${name || "transcripcion"}.${ext}`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function formatDuration(seconds) {
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
