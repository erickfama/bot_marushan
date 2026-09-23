# Marushan Music Bot

Bot musical personal para Discord. Usa `discord.py` y Wavelink para controlar un nodo Lavalink v4. Las búsquedas y enlaces de YouTube aportan el audio; Spotify se usa únicamente como catálogo para importar canciones, álbumes y playlists privadas. El bot resuelve el stream justo antes de reproducirlo con `yt-dlp` y Deno, mientras que un servicio interno `yt-cipher` respalda la integración de Lavalink. No se usan cookies personales.

## Funciones

- Cola de hasta 500 pistas, inserción siguiente, páginas, mover, eliminar, limpiar y mezclar.
- Pause, resume, skip, previous, jump, seek, volumen 1–150, loop de canción/cola y autoplay.
- YouTube, playlists de YouTube, radio/HTTP, adjuntos de Discord y archivos del directorio `media/`.
- Spotify OAuth para tracks, álbumes y playlists privadas; resolución de cada pista contra YouTube.
- Slash commands en un servidor y comandos `!` de compatibilidad.
- Controles mediante botones y desconexión por inactividad.
- Docker Compose con reinicio automático, healthchecks y rotación de logs.

La cola, historial y ajustes de sesión viven en memoria y se limpian al reiniciar.

## Requisitos

- Una aplicación/bot de Discord con los intents `Message Content` y `Server Members` según corresponda.
- Permisos en Discord: View Channel, Send Messages, Embed Links, Connect y Speak.
- Una aplicación de Spotify en modo desarrollo para importar contenido privado (opcional).
- Docker Engine con Docker Compose para producción, o Python 3.11+ y un Lavalink v4 para desarrollo.

## Configuración local

1. Copia `.env.example` a `.env` y completa `DISCORD_GUILD_ID` y `SPOTIFY_CLIENT_ID`.
2. Crea `secrets/` y estos archivos de una sola línea:

   - `discord_token.txt`
   - `lavalink_password.txt` (usa una contraseña aleatoria larga)
   - `spotify_client_secret.txt`
   - `spotify_refresh_token.txt`

   Si no usarás Spotify todavía, crea vacíos los dos últimos archivos.

3. En Discord Developer Portal activa Message Content Intent. Invita el bot con los scopes `bot` y `applications.commands`.
4. Arranca el stack:

   ```bash
   docker compose up --build -d
   docker compose ps
   docker compose logs -f bot lavalink yt-cipher
   ```

Lavalink y `yt-cipher` no publican puertos al host; únicamente los servicios de la red interna de Compose pueden alcanzarlos.

### OAuth de Spotify

En Spotify Developer Dashboard registra exactamente `http://127.0.0.1:8765/callback` como Redirect URI. Instala las dependencias y ejecuta:

```bash
python scripts/spotify_oauth.py --client-id TU_CLIENT_ID
```

El script abre el navegador, solicita `playlist-read-private`, `playlist-read-collaborative` y `user-library-read`, y muestra el refresh token. Guárdalo en `secrets/spotify_refresh_token.txt`; nunca lo confirmes en Git.

Spotify no se usa para retransmitir audio. Sus metadatos conservan el enlace de atribución y la canción se empareja con un resultado reproducible de YouTube.

## Comandos

Los comandos principales son `/play`, `/play-file`, `/queue`, `/nowplaying`, `/pause`, `/resume`, `/skip`, `/previous`, `/stop`, `/disconnect`, `/remove`, `/move`, `/clear`, `/shuffle`, `/jump`, `/seek`, `/volume`, `/loop`, `/autoplay` y `/join`.

Ejemplos:

```text
/play consulta:Radiohead Creep
/play consulta:https://open.spotify.com/playlist/... siguiente:false
/seek tiempo:1:30
/loop modo:queue
```

Los aliases con `!` siguen disponibles durante la transición. `!play_local archivo.mp3` solo admite rutas relativas dentro de `media/`.

## Pruebas

```bash
python -m pytest -q
python -m compileall -q src scripts
docker compose config --quiet
```

## Despliegue en IONOS

Antes de modificar el VPS, copia y ejecuta `scripts/vps_preflight.sh`; solo consulta sistema, recursos, Docker, contenedores, redes y puertos. Revisa el resultado para elegir un directorio independiente que no interfiera con los proyectos existentes.

Luego clona la rama, crea `.env` y `secrets/` directamente en el servidor y ejecuta `docker compose up --build -d`. La política `unless-stopped` levanta bot y Lavalink después de reiniciar Docker o el VPS.

No reinicies el VPS sin una ventana acordada. La validación normal puede hacerse con `docker compose restart`, seguida de `docker compose ps` y los logs.

## Solución de problemas

- **Los slash commands no aparecen:** confirma `DISCORD_GUILD_ID`, el scope `applications.commands` y reinicia el bot.
- **Lavalink no está healthy:** revisa `secrets/lavalink_password.txt` y consulta sus logs.
- **Spotify devuelve 401/403:** repite la autorización y verifica que tu usuario esté autorizado en la aplicación en modo desarrollo.
- **Una pista de Spotify no se agrega:** no se encontró una coincidencia suficientemente confiable en YouTube; el bot la contabiliza como omitida.
- **YouTube cambia o bloquea una fuente:** revisa primero los logs de `lavalink` y `yt-cipher`. Actualiza de forma controlada sus imágenes o el plugin declarado en `lavalink/application.yml`; no agregues cookies de tu cuenta principal.
