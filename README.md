# Descargador de YouTube

Programa sencillo para Windows que descarga videos de YouTube en la mejor calidad disponible. Pegas el enlace, pulsas **Descargar** y el video queda en tu carpeta Descargas.

## Qué hace

- Descarga en la máxima calidad que tenga el video (hasta 4K), o en 1080p, 720p o 480p.
- Puede guardar solo el audio en MP3.
- Puede descargar una lista de reproducción completa en su propia subcarpeta numerada.
- Al terminar abre la carpeta con el video ya seleccionado.
- Si YouTube rechaza la descarga (error 403), reintenta solo hasta 4 veces.

## Requisitos

- Windows 10 u 11.
- [Python 3.10 o más nuevo](https://www.python.org/downloads/), marcando "Add python.exe to PATH" al instalarlo.

Lo demás (`yt-dlp`, `ffmpeg` y `deno`) lo instala el programa la primera vez que se abre.

## Cómo usarlo

1. Descarga `descargador.py` y `Descargar YouTube.bat` en la misma carpeta.
2. Haz doble clic en `Descargar YouTube.bat`.
3. Pega el enlace del video, elige la calidad y pulsa **Descargar**.

Desde la consola también funciona:

```
python descargador.py ENLACE -c 1080
```

Opciones: `-c max|1080|720|480|mp3` (calidad), `-o CARPETA` (dónde guardar), `-l` (lista completa).

## Si deja de funcionar

YouTube cambia a menudo. Pulsa **Actualizar** en la ventana y vuelve a abrir el programa.

## Aviso

Usa este programa solo con videos que tengas derecho a descargar.
