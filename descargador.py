# -*- coding: utf-8 -*-
"""
Descargador de YouTube
======================

Ventana:   doble clic en "Descargar YouTube.bat" (o: python descargador.py)
Consola:   python descargador.py ENLACE [-c max|1080|720|480|mp3] [-o CARPETA] [-l]

Usa yt-dlp para descargar y ffmpeg para unir el mejor video con el mejor audio.
Si falta algun componente, se instala solo la primera vez.
"""
from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

INTENTOS = 4  # veces que se reintenta si YouTube rechaza la descarga (error 403)
PAQUETES =["yt-dlp[default]", "imageio-ffmpeg", "deno"]
PAQUETES_MINIMOS = ["yt-dlp", "yt-dlp-ejs", "imageio-ffmpeg", "deno"]

AQUI = Path(__file__).resolve().parent
ARCHIVO_CONFIG = AQUI / "config.json"


def carpeta_descargas() -> Path:
    """La carpeta Descargas de Windows, aunque el usuario la haya movido de sitio."""
    if os.name == "nt":
        try:
            import winreg
            clave = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, clave) as k:
                valor, _ = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
            ruta = Path(os.path.expandvars(valor))
            if ruta.is_dir():
                return ruta
        except Exception:
            pass
    return Path.home() / "Downloads"


CARPETA_DEFECTO = carpeta_descargas()


def dentro_de_descargas(ruta) -> str | None:
    """'Descargas' (como la llama Windows) si la ruta es esa carpeta o esta dentro."""
    try:
        resto = Path(ruta).expanduser().resolve().relative_to(CARPETA_DEFECTO.resolve())
    except Exception:
        return None
    return "Descargas" if str(resto) == "." else "Descargas\\" + str(resto)


def nombre_amigable(ruta) -> str:
    return dentro_de_descargas(ruta) or str(ruta)

# clave -> (texto que ve el usuario, altura maxima o None)
CALIDADES = {
    "max": ("Máxima calidad (4K / 2K si el video la tiene)", None),
    "1080": ("1080p  ·  Full HD", 1080),
    "720": ("720p  ·  HD", 720),
    "480": ("480p  ·  archivo más liviano", 480),
    "mp3": ("Solo audio (MP3)", None),
}


# --------------------------------------------------------------------------
# Componentes (yt-dlp, ffmpeg, deno)
# --------------------------------------------------------------------------

def _sin_ventana() -> dict:
    """Evita que aparezca una consola negra al lanzar procesos en Windows."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _python() -> str:
    """python.exe aunque estemos corriendo con pythonw.exe."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def instalar_componentes() -> tuple[bool, str]:
    """Instala o actualiza yt-dlp, ffmpeg y deno con pip."""
    base = [_python(), "-m", "pip", "install", "--upgrade", "--disable-pip-version-check"]
    salida = ""
    for paquetes in (PAQUETES, PAQUETES_MINIMOS):
        r = subprocess.run(base + paquetes, capture_output=True, text=True,
                           errors="replace", **_sin_ventana())
        salida = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            importlib.invalidate_caches()
            return True, salida
    return False, salida[-1500:]


def cargar_ytdlp(avisar=print):
    """Importa yt-dlp; si no esta, lo instala primero."""
    try:
        import yt_dlp
        import imageio_ffmpeg  # noqa: F401
    except ImportError:
        avisar("Instalando componentes (solo la primera vez, espera un momento)…")
        ok, salida = instalar_componentes()
        if not ok:
            raise RuntimeError("No se pudieron instalar los componentes:\n" + salida)
        import yt_dlp
    return yt_dlp


def ruta_ffmpeg() -> str | None:
    """Ruta a ffmpeg si NO esta en el PATH (si esta, yt-dlp lo encuentra solo)."""
    if shutil.which("ffmpeg"):
        return None
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ruta_deno() -> str | None:
    """YouTube exige un motor de JavaScript para entregar todas las calidades."""
    en_path = shutil.which("deno")
    if en_path:
        return en_path
    try:
        import deno
        return deno.find_deno_bin()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Descarga
# --------------------------------------------------------------------------

def _tamano(n) -> str:
    if not n:
        return "?"
    n = float(n)
    for unidad in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidad == "GB":
            return f"{n:.0f} {unidad}" if unidad in ("B", "KB") else f"{n:.1f} {unidad}"
        n /= 1024
    return "?"


def _tiempo(seg) -> str:
    if seg is None:
        return "?"
    seg = int(seg)
    h, resto = divmod(seg, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _Registro:
    """Recibe los mensajes de yt-dlp cuando hay ventana (sin consola)."""

    def __init__(self):
        self.errores: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.errores.append(str(msg).replace("ERROR: ", "").strip())


def _recoger_archivos(info, salida: list):
    """Saca de la respuesta de yt-dlp los archivos finales guardados."""
    if not info:
        return
    if info.get("_type") == "playlist" or "entries" in info:
        for entrada in info.get("entries") or []:
            _recoger_archivos(entrada, salida)
        return
    for d in info.get("requested_downloads") or []:
        if d.get("filepath"):
            alto = d.get("height") or info.get("height")
            salida.append((d["filepath"], f"{alto}p" if alto else "audio"))


def descargar(yt_dlp, url: str, calidad: str = "max", carpeta: Path = CARPETA_DEFECTO,
              lista: bool = False, emitir=None, cancelar: threading.Event | None = None):
    """
    Descarga `url` y devuelve (archivos, errores).
      archivos: lista de (ruta, calidad obtenida)
      emitir:   funcion que recibe eventos para la ventana; None = modo consola
    """
    carpeta = Path(carpeta).expanduser()
    carpeta.mkdir(parents=True, exist_ok=True)
    con_ventana = emitir is not None
    registro = _Registro()

    comunes: dict = {
        "noplaylist": not lista,
        "windowsfilenames": os.name == "nt",
        "retries": 10,
        "fragment_retries": 10,
    }
    if con_ventana:
        comunes.update(quiet=True, noprogress=True, logger=registro, color="never")
    ffmpeg, deno = ruta_ffmpeg(), ruta_deno()
    if ffmpeg:
        comunes["ffmpeg_location"] = ffmpeg
    if deno:
        comunes["js_runtimes"] = {"deno": {"path": deno}}

    # Si pidieron la lista completa, miramos primero si de verdad es una lista
    # para guardarla en su propia subcarpeta, numerada.
    plantilla = "%(title).150B.%(ext)s"
    if lista:
        if con_ventana:
            emitir(("estado", "Leyendo la lista de reproducción…"))
        with yt_dlp.YoutubeDL({**comunes, "extract_flat": "in_playlist"}) as y:
            previo = y.extract_info(url, download=False)
        if previo and previo.get("_type") == "playlist":
            titulo = yt_dlp.utils.sanitize_filename(previo.get("title") or "Lista")
            carpeta = carpeta / (titulo[:80].strip(" .") or "Lista")
            plantilla = "%(playlist_index)02d - " + plantilla
            comunes["ignoreerrors"] = "only_download"  # un video caido no frena la lista
            registro.errores.clear()

    opciones = {**comunes, "paths": {"home": str(carpeta)}, "outtmpl": {"default": plantilla}}
    if calidad == "mp3":
        opciones["format"] = "ba/b"
        opciones["final_ext"] = "mp3"  # para reconocer un MP3 que ya estaba bajado
        opciones["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
        ]
    else:
        alto = CALIDADES[calidad][1]
        # Mejor video + mejor audio. Primero manda la resolucion y los fps; a
        # igual calidad se prefiere H.264 + AAC, que da un MP4 que se abre en
        # cualquier reproductor. En 4K YouTube solo ofrece VP9/AV1 (sale MKV/MP4).
        opciones["format"] = "bv*+ba/b"
        opciones["format_sort"] = ["lang", f"res:{alto}" if alto else "res", "fps",
                                   "vcodec:h264", "acodec:aac"]
        opciones["merge_output_format"] = "mp4/mkv"

    temporales: set = set()  # archivos .part a borrar si se cancela
    if con_ventana:
        vistos: set = set()

        def al_progreso(d):
            parcial = d.get("tmpfilename") or ""
            if parcial.endswith(".part"):
                temporales.update((parcial, parcial + ".ytdl"))
            if cancelar is not None and cancelar.is_set():
                raise yt_dlp.utils.DownloadCancelled("Descarga cancelada")
            info = d.get("info_dict") or {}
            if info.get("id") not in vistos:
                vistos.add(info.get("id"))
                emitir(("log", "▶ " + str(info.get("title") or url)))
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            hecho = d.get("downloaded_bytes") or 0
            pct = min(100.0, hecho * 100 / total) if total else None
            parte = "audio" if info.get("vcodec") == "none" else "video"
            prefijo = ""
            if info.get("playlist_index") and (info.get("n_entries") or 0) > 1:
                prefijo = f"[{info['playlist_index']}/{info['n_entries']}]  "
            texto = f"{prefijo}Descargando {parte}"
            if pct is not None:
                texto += f"  {pct:.0f}%"
            texto += f"  ·  {_tamano(hecho)} de {_tamano(total)}"
            if d.get("speed"):
                texto += f"  ·  {_tamano(d['speed'])}/s"
            if d.get("eta") is not None:
                texto += f"  ·  faltan {_tiempo(d['eta'])}"
            emitir(("progreso", pct, texto))

        def al_procesar(d):
            if cancelar is not None and cancelar.is_set():
                raise yt_dlp.utils.DownloadCancelled("Descarga cancelada")
            if d.get("status") == "started":
                paso = {"Merger": "Uniendo video y audio…",
                        "ExtractAudio": "Convirtiendo a MP3…"}.get(d.get("postprocessor"))
                if paso:
                    emitir(("progreso", 100.0, paso))

        opciones["progress_hooks"] = [al_progreso]
        opciones["postprocessor_hooks"] = [al_procesar]

    # YouTube a veces rechaza una descarga con "403 Forbidden" sin motivo; pedir
    # el video de nuevo lo arregla. Lo ya descargado no se vuelve a bajar.
    archivos: list = []
    cancelado = False
    try:
        for intento in range(1, INTENTOS + 1):
            ultimo = intento == INTENTOS
            registro.errores.clear()
            archivos.clear()
            try:
                with yt_dlp.YoutubeDL(opciones) as ydl:
                    info = ydl.extract_info(url, download=True)
            except yt_dlp.utils.DownloadError as e:
                if ultimo or "403" not in str(e):
                    raise
            else:
                _recoger_archivos(info, archivos)
                if ultimo or not any("403" in e for e in registro.errores):
                    break
            aviso = f"YouTube rechazó la descarga; reintentando ({intento + 1} de {INTENTOS})…"
            emitir(("estado", aviso)) if con_ventana else print("\n" + aviso)
            time.sleep(2)
    except yt_dlp.utils.DownloadCancelled:
        cancelado = True
    if cancelado:
        # Fuera del except: asi Windows ya solto los archivos a medio bajar.
        gc.collect()
        for parcial in temporales:
            try:
                os.remove(parcial)
            except OSError:
                pass
        raise yt_dlp.utils.DownloadCancelled("Descarga cancelada")
    return archivos, registro.errores


def explicar_error(texto: str) -> str:
    """Convierte los errores tecnicos mas comunes en algo entendible."""
    t = texto.lower()
    if "not a bot" in t or "sign in to confirm" in t:
        return ("YouTube pidió verificar que no eres un robot. Espera unos minutos "
                "y vuelve a intentarlo.")
    if "private video" in t or "unavailable" in t or "has been removed" in t:
        return "El video no está disponible (es privado, fue borrado o está bloqueado en tu país)."
    if "confirm your age" in t or "age-restricted" in t:
        return "El video tiene restricción de edad y YouTube exige iniciar sesión para verlo."
    if "unsupported url" in t or "is not a valid url" in t or "unable to recognize" in t:
        return "El enlace no parece válido. Copia la dirección completa del video."
    if "getaddrinfo" in t or "timed out" in t or "connection" in t or "urlopen error" in t:
        return "No hay conexión con YouTube. Revisa tu internet y vuelve a intentarlo."
    if "requested format is not available" in t or "nsig" in t or "signature" in t:
        return ("YouTube cambió algo y el descargador quedó desactualizado. "
                "Pulsa «Actualizar» y vuelve a abrir el programa.")
    return texto.strip() + "\n\nSi el problema sigue, pulsa «Actualizar» y vuelve a abrir el programa."


# --------------------------------------------------------------------------
# Configuracion recordada (carpeta y calidad)
# --------------------------------------------------------------------------

def leer_config() -> dict:
    try:
        return json.loads(ARCHIVO_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def guardar_config(datos: dict):
    try:
        ARCHIVO_CONFIG.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Ventana
# --------------------------------------------------------------------------

def modo_ventana():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    if os.name == "nt":  # texto nitido en pantallas con escala
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    ROJO, ROJO_OSCURO, GRIS = "#d32f2f", "#b71c1c", "#666666"
    FUENTE = "Segoe UI" if os.name == "nt" else "TkDefaultFont"

    class App:
        def __init__(self, raiz: tk.Tk):
            self.raiz = raiz
            self.cola: queue.Queue = queue.Queue()
            self.cancelar = threading.Event()
            self.ocupado = False
            cfg = leer_config()

            raiz.title("Descargador de YouTube")
            raiz.minsize(640, 590)
            ttk.Style().configure(".", font=(FUENTE, 10))

            m = ttk.Frame(raiz, padding=18)
            m.pack(fill="both", expand=True)
            m.columnconfigure(0, weight=1)

            ttk.Label(m, text="Descargador de YouTube", font=(FUENTE, 17, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w")
            ttk.Label(m, text="Pega el enlace del video y pulsa Descargar.", foreground=GRIS).grid(
                row=1, column=0, columnspan=3, sticky="w")

            ttk.Label(m, text="Enlace del video").grid(row=2, column=0, sticky="w", pady=(16, 4))
            self.url = tk.StringVar()
            self.entrada = ttk.Entry(m, textvariable=self.url, font=(FUENTE, 11))
            self.entrada.grid(row=3, column=0, columnspan=2, sticky="ew", ipady=3)
            ttk.Button(m, text="Pegar", command=self.pegar).grid(row=3, column=2, padx=(8, 0))

            ttk.Label(m, text="Calidad").grid(row=4, column=0, sticky="w", pady=(14, 4))
            self.textos = [t for t, _ in CALIDADES.values()]
            self.calidad = ttk.Combobox(m, values=self.textos, state="readonly", font=(FUENTE, 10))
            clave = cfg.get("calidad") if cfg.get("calidad") in CALIDADES else "max"
            self.calidad.current(list(CALIDADES).index(clave))
            self.calidad.grid(row=5, column=0, columnspan=3, sticky="ew")

            self.lista = tk.BooleanVar(value=False)
            ttk.Checkbutton(m, text="Descargar la lista de reproducción completa (si el enlace es de una lista)",
                            variable=self.lista).grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))

            ttk.Label(m, text="Guardar en").grid(row=7, column=0, sticky="w", pady=(14, 4))
            self.carpeta = tk.StringVar(value=cfg.get("carpeta") or str(CARPETA_DEFECTO))
            ttk.Entry(m, textvariable=self.carpeta).grid(row=8, column=0, sticky="ew", ipady=2)
            ttk.Button(m, text="Cambiar…", command=self.elegir_carpeta).grid(row=8, column=1, padx=(8, 0))
            ttk.Button(m, text="Abrir", command=self.abrir_carpeta).grid(row=8, column=2, padx=(8, 0))
            self.pista = ttk.Label(m, text="", foreground=GRIS, font=(FUENTE, 9))
            self.pista.grid(row=9, column=0, columnspan=3, sticky="w", pady=(3, 0))
            self.carpeta.trace_add("write", self.actualizar_pista)
            self.actualizar_pista()

            self.abrir_fin = tk.BooleanVar(value=bool(cfg.get("abrir_al_terminar", True)))
            ttk.Checkbutton(m, text="Al terminar, abrir la carpeta con el video ya seleccionado",
                            variable=self.abrir_fin).grid(row=10, column=0, columnspan=3, sticky="w", pady=(8, 0))

            botones = ttk.Frame(m)
            botones.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(18, 10))
            botones.columnconfigure(0, weight=1)
            self.boton = tk.Button(botones, text="⬇  Descargar", command=self.pulsar,
                                   bg=ROJO, fg="white", activebackground=ROJO_OSCURO,
                                   activeforeground="white", relief="flat", cursor="hand2",
                                   font=(FUENTE, 12, "bold"), pady=8)
            self.boton.grid(row=0, column=0, sticky="ew")
            self.boton_actualizar = ttk.Button(botones, text="Actualizar", command=self.actualizar)
            self.boton_actualizar.grid(row=0, column=1, padx=(8, 0), sticky="ns")

            self.barra = ttk.Progressbar(m, maximum=100)
            self.barra.grid(row=12, column=0, columnspan=3, sticky="ew")
            self.estado = ttk.Label(m, text="Listo para descargar.", foreground=GRIS, wraplength=600)
            self.estado.grid(row=13, column=0, columnspan=3, sticky="w", pady=(6, 8))

            self.log = tk.Text(m, height=7, wrap="word", state="disabled", relief="flat",
                               bg="#f4f4f4", font=(FUENTE, 9), padx=8, pady=6)
            self.log.grid(row=14, column=0, columnspan=3, sticky="nsew")
            m.rowconfigure(14, weight=1)

            self.entrada.bind("<Return>", lambda e: self.pulsar())
            raiz.protocol("WM_DELETE_WINDOW", self.cerrar)
            self.pegar(silencioso=True)
            self.entrada.focus_set()
            raiz.after(100, self.atender_cola)

        # ---- acciones -----------------------------------------------------
        def pegar(self, silencioso=False):
            try:
                texto = self.raiz.clipboard_get().strip()
            except Exception:
                return
            if silencioso and not ("youtube.com" in texto or "youtu.be" in texto):
                return
            if texto and "\n" not in texto:
                self.url.set(texto)

        def elegir_carpeta(self):
            elegida = filedialog.askdirectory(initialdir=self.carpeta.get() or str(Path.home()),
                                              title="¿Dónde guardo los videos?")
            if elegida:
                self.carpeta.set(str(Path(elegida)))

        def abrir_carpeta(self):
            carpeta = Path(self.carpeta.get()).expanduser()
            try:
                carpeta.mkdir(parents=True, exist_ok=True)
                os.startfile(carpeta) if os.name == "nt" else subprocess.Popen(["xdg-open", str(carpeta)])
            except Exception as e:
                messagebox.showerror("No se pudo abrir la carpeta", str(e))

        def actualizar_pista(self, *_):
            """Aclara que C:\\Users\\...\\Downloads es la carpeta Descargas de Windows."""
            texto = self.carpeta.get().strip()
            amigable = dentro_de_descargas(texto) if texto else None
            self.pista.config(text=f"Es tu carpeta «{amigable}» de Windows." if amigable else "")

        def mostrar_en_carpeta(self, ruta, solo_carpeta=False):
            """Abre el Explorador con el archivo recien bajado ya seleccionado."""
            try:
                p = Path(ruta)
                if os.name != "nt":
                    subprocess.Popen(["xdg-open", str(p.parent)])
                elif solo_carpeta or not p.exists():
                    os.startfile(p.parent)
                else:
                    subprocess.Popen(f'explorer /select,"{p}"')
            except Exception:
                pass

        def clave_calidad(self) -> str:
            return list(CALIDADES)[max(0, self.calidad.current())]

        def pulsar(self):
            if self.ocupado:
                self.cancelar.set()
                self.poner_estado("Cancelando…")
                self.boton.config(state="disabled")
                return
            url = self.url.get().strip()
            if not url:
                messagebox.showinfo("Falta el enlace", "Pega primero el enlace del video de YouTube.")
                return
            if not url.lower().startswith(("http://", "https://")):
                url = "https://" + url
            carpeta = self.carpeta.get().strip() or str(CARPETA_DEFECTO)
            guardar_config({"carpeta": carpeta, "calidad": self.clave_calidad(),
                            "abrir_al_terminar": bool(self.abrir_fin.get())})
            self.cancelar.clear()
            self.en_marcha(True)
            self.barra.config(mode="indeterminate")
            self.barra.start(12)
            self.poner_estado("Buscando el video…")
            threading.Thread(target=self.trabajo_descarga, daemon=True,
                             args=(url, self.clave_calidad(), Path(carpeta), self.lista.get())).start()

        def actualizar(self):
            if self.ocupado:
                return
            self.en_marcha(True, cancelable=False)
            self.barra.config(mode="indeterminate")
            self.barra.start(12)
            self.poner_estado("Actualizando componentes…")
            threading.Thread(target=self.trabajo_actualizar, daemon=True).start()

        def cerrar(self):
            if self.ocupado and not messagebox.askyesno(
                    "Descarga en curso", "Hay una descarga en curso. ¿Cerrar de todos modos?"):
                return
            self.cancelar.set()
            self.raiz.destroy()

        # ---- trabajo en segundo plano ---------------------------------------
        def trabajo_descarga(self, url, calidad, carpeta, lista):
            poner = self.cola.put
            try:
                yt_dlp = cargar_ytdlp(lambda t: poner(("estado", t)))
                archivos, errores = descargar(yt_dlp, url, calidad, carpeta, lista,
                                              emitir=poner, cancelar=self.cancelar)
                for ruta, obtenido in archivos:
                    p = Path(ruta)
                    peso = _tamano(p.stat().st_size) if p.exists() else "?"
                    poner(("log", f"✔ {p.name}   ({obtenido}, {peso})"))
                for e in errores:
                    poner(("log", "✖ " + e))
                if archivos:
                    primero = Path(archivos[0][0])
                    lugar = nombre_amigable(primero.parent)
                    extra = f" ({len(errores)} con error)" if errores else ""
                    if len(archivos) == 1:
                        mensaje = f"✔ Descargado. Está en {lugar}: {primero.name}"
                    else:
                        mensaje = f"✔ Descargados {len(archivos)} archivos{extra}. Están en {lugar}"
                    poner(("fin", True, mensaje, str(primero), len(archivos)))
                else:
                    poner(("fin", False, explicar_error(errores[-1] if errores else "No se descargó nada.")))
            except Exception as e:
                if self.cancelar.is_set() or type(e).__name__ == "DownloadCancelled":
                    poner(("fin", None, "Descarga cancelada."))
                else:
                    poner(("fin", False, explicar_error(str(e).replace("ERROR: ", ""))))

        def trabajo_actualizar(self):
            ok, salida = instalar_componentes()
            if ok:
                self.cola.put(("fin", True, "✔ Componentes actualizados. Cierra y vuelve a abrir el programa."))
            else:
                self.cola.put(("fin", False, "No se pudo actualizar:\n" + salida[-400:]))

        # ---- interfaz -----------------------------------------------------
        def en_marcha(self, ocupado: bool, cancelable: bool = True):
            self.ocupado = ocupado
            if ocupado:
                self.boton.config(text="✖  Cancelar" if cancelable else "Espera…", bg="#555555",
                                  activebackground="#333333",
                                  state="normal" if cancelable else "disabled")
                self.boton_actualizar.state(["disabled"])
            else:
                self.boton.config(text="⬇  Descargar", bg=ROJO, activebackground=ROJO_OSCURO, state="normal")
                self.boton_actualizar.state(["!disabled"])

        def poner_estado(self, texto, color=GRIS):
            self.estado.config(text=texto, foreground=color)

        def escribir_log(self, texto):
            self.log.config(state="normal")
            self.log.insert("end", texto + "\n")
            self.log.see("end")
            self.log.config(state="disabled")

        def barra_fija(self, valor):
            self.barra.stop()
            self.barra.config(mode="determinate", value=valor)

        def atender_cola(self):
            try:
                while True:
                    evento = self.cola.get_nowait()
                    tipo = evento[0]
                    if tipo == "estado":
                        self.poner_estado(evento[1])
                    elif tipo == "log":
                        self.escribir_log(evento[1])
                    elif tipo == "progreso":
                        if evento[1] is not None:
                            self.barra_fija(evento[1])
                        self.poner_estado(evento[2])
                    elif tipo == "fin":
                        bien, mensaje = evento[1], evento[2]
                        self.en_marcha(False)
                        self.barra_fija(100 if bien else 0)
                        color = {True: "#1b7f3b", False: ROJO}.get(bien, GRIS)
                        self.poner_estado(mensaje.split("\n")[0], color)
                        if bien is False:
                            self.escribir_log("✖ " + mensaje)
                            messagebox.showerror("No se pudo descargar", mensaje)
                        elif bien:
                            self.url.set("")
                            if len(evento) > 3 and self.abrir_fin.get():
                                self.mostrar_en_carpeta(evento[3], solo_carpeta=evento[4] > 1)
            except queue.Empty:
                pass
            self.raiz.after(100, self.atender_cola)

    raiz = tk.Tk()
    raiz.app = App(raiz)
    raiz.mainloop()


# --------------------------------------------------------------------------
# Consola
# --------------------------------------------------------------------------

def modo_consola(argumentos: list[str]) -> int:
    p = argparse.ArgumentParser(prog="descargador.py", description="Descargador de YouTube")
    p.add_argument("enlaces", nargs="+", help="uno o varios enlaces de YouTube")
    p.add_argument("-c", "--calidad", choices=list(CALIDADES), default="max",
                   help="max (por defecto), 1080, 720, 480 o mp3")
    p.add_argument("-o", "--carpeta", default=str(CARPETA_DEFECTO), help="donde guardar")
    p.add_argument("-l", "--lista", action="store_true", help="descargar la lista completa")
    a = p.parse_args(argumentos)

    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(errors="replace")
        except Exception:
            pass

    yt_dlp = cargar_ytdlp()
    fallos = 0
    for enlace in a.enlaces:
        try:
            archivos, _ = descargar(yt_dlp, enlace, a.calidad, Path(a.carpeta), a.lista)
        except Exception as e:
            print("\nNo se pudo descargar:", explicar_error(str(e).replace("ERROR: ", "")))
            fallos += 1
            continue
        if not archivos:
            fallos += 1
        for ruta, obtenido in archivos:
            p_ = Path(ruta)
            peso = _tamano(p_.stat().st_size) if p_.exists() else "?"
            print(f"\nGuardado ({obtenido}, {peso}): {ruta}")
    return 1 if fallos else 0


def main():
    if len(sys.argv) > 1:
        sys.exit(modo_consola(sys.argv[1:]))
    # Con pythonw no hay consola: evita fallos de librerias que intenten escribir.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        modo_ventana()
    except Exception as e:  # que un fallo al abrir no pase en silencio
        try:
            from tkinter import messagebox
            messagebox.showerror("Descargador de YouTube", f"No se pudo abrir el programa:\n\n{e}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
