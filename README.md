# Bot Nissin

Bot musical personal para Discord. Usa `discord.py` y Wavelink para controlar un nodo Lavalink v4. Las búsquedas y enlaces de YouTube aportan el audio; Spotify se usa únicamente como catálogo para importar canciones, álbumes y playlists privadas. Lavalink reproduce las pistas mediante su plugin de YouTube y el servicio interno `yt-cipher`. No se usan cookies personales ni URLs temporales de Google Video.

## Funciones

- Cola de hasta 500 pistas, inserción siguiente, páginas, mover, eliminar, limpiar y mezclar.
- Pause, resume, skip, previous, jump, seek, volumen 1–150, loop de canción/cola y autoplay.
- YouTube, playlists de YouTube, radio/HTTP, adjuntos de Discord y archivos del directorio `media/`.
- Enlaces públicos de canciones de Spotify sin OAuth; OAuth para álbumes, playlists y biblioteca privada. Cada pista se resuelve contra YouTube.
- Slash commands en un servidor y comandos `!` de compatibilidad.
- Controles mediante botones y desconexión por inactividad.
- Docker Compose con reinicio automático, healthchecks y rotación de logs.
- Reproductor web responsive en `music.orza.mx`, protegido con login de Discord y estado en vivo por WebSocket.
- Biblioteca persistente con favoritos, historial, preferencias y recuperación de la cola tras reiniciar.
- Centro de importaciones con progreso, cancelación, reintento y registro de trabajos anteriores.
- Búsqueda web con selección de resultados antes de agregarlos.
- Estadísticas de los últimos 30 días y diagnóstico separado de Discord, Lavalink, Spotify y SQLite.

Los datos musicales viven en SQLite dentro del volumen Docker `bot-data`. Las sesiones de inicio de sesión web siguen siendo temporales y se cierran al reiniciar.

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
   - `discord_oauth_client_secret.txt`
   - `web_session_secret.txt`

   Si no usarás Spotify todavía, deja vacíos `spotify_client_secret.txt` y `spotify_refresh_token.txt`. El panel web mantendrá el login cerrado mientras sus credenciales OAuth estén vacías.

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
python scripts/spotify_oauth.py --client-id TU_CLIENT_ID \
  --client-secret-file secrets/spotify_client_secret.txt \
  --output secrets/spotify_refresh_token.txt
```

El script abre el navegador, solicita `playlist-read-private`, `playlist-read-collaborative` y `user-library-read`, y guarda el refresh token directamente en `secrets/spotify_refresh_token.txt`; nunca lo confirmes en Git ni lo pegues en el chat.

Spotify no se usa para retransmitir audio. Sus metadatos conservan el enlace de atribución y la canción se empareja con un resultado reproducible de YouTube. Los enlaces públicos de canciones funcionan sin credenciales; los álbumes y playlists requieren la configuración OAuth anterior.

Desde los cambios de Spotify Web API de 2026, una aplicación en modo desarrollo solo puede leer los elementos de playlists que la cuenta autorizada posee o en las que colabora. Para una playlist ajena, cópiala primero a una playlist propia. La resolución hacia YouTube usa hasta cuatro búsquedas concurrentes, pero mantiene el orden original.

### Reproductor web

El frontend React se compila dentro de la imagen y FastAPI lo sirve desde el mismo proceso del bot. Node no se ejecuta en producción. La API escucha en `127.0.0.1:8090` a través del mapeo de Compose; Lavalink sigue aislado en la red interna.

1. En Discord Developer Portal agrega la redirección exacta `https://music.orza.mx/auth/discord/callback`.
2. Coloca el client ID en `DISCORD_OAUTH_CLIENT_ID` y el client secret en `secrets/discord_oauth_client_secret.txt`.
3. Genera un valor aleatorio largo para `secrets/web_session_secret.txt`.
4. Define `PUBLIC_BASE_URL=https://music.orza.mx`.
5. Crea el registro DNS `A` de `music.orza.mx` hacia el VPS.
6. Copia `deploy/nginx/music.orza.mx.conf` a los sitios de Nginx, valida con `nginx -t` y emite el certificado con Certbot.

El panel permite ver el estado a cualquier miembro autenticado del servidor. Los controles solo se habilitan cuando ese miembro está dentro del canal de voz seleccionado. La cola, historial, favoritos, estadísticas e importaciones se conservan; solamente las sesiones web se limpian al reiniciar.

La navegación del panel contiene Reproductor, Biblioteca, Importaciones, Historial, Estadísticas y Estado. Las estadísticas se generan localmente y no se comparten con servicios externos.

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

Luego clona la rama, crea `.env` y `secrets/` directamente en el servidor y ejecuta `docker compose up --build -d`. La política `unless-stopped` levanta bot y Lavalink después de reiniciar Docker o el VPS. Compose crea el volumen `bot-data`; no lo elimines durante una actualización porque contiene `music.db`.

No reinicies el VPS sin una ventana acordada. La validación normal puede hacerse con `docker compose restart`, seguida de `docker compose ps` y los logs.

## Solución de problemas

- **Los slash commands no aparecen:** confirma `DISCORD_GUILD_ID`, el scope `applications.commands` y reinicia el bot.
- **Lavalink no está healthy:** revisa `secrets/lavalink_password.txt` y consulta sus logs.
- **Spotify devuelve 401/403:** repite la autorización y verifica que tu usuario esté autorizado en la aplicación en modo desarrollo.
- **Spotify no muestra las canciones de una playlist:** confirma que tu cuenta sea propietaria o colaboradora; seguir una playlist pública ajena ya no concede acceso a sus elementos.
- **El login web devuelve 503:** configura el client ID y el secret OAuth de Discord y vuelve a crear solo el contenedor `bot`.
- **El panel abre pero no permite controlar:** entra desde Discord al mismo canal seleccionado en la web.
- **Una pista de Spotify no se agrega:** no se encontró una coincidencia suficientemente confiable en YouTube; el bot la contabiliza como omitida.
- **Una canción individual de Spotify funciona, pero un álbum o playlist no:** las canciones públicas pueden usar metadatos abiertos; los álbumes y playlists requieren las tres variables OAuth de Spotify (`SPOTIFY_CLIENT_ID`, client secret y refresh token).
- **El bot entra al canal pero no se escucha:** confirma que su rol tenga `View Channel`, `Connect` y `Speak`. El bot ahora valida esos permisos y supervisa que la posición de Lavalink avance; si una fuente queda atascada, la omite y continúa la cola.
- **YouTube cambia o bloquea una fuente:** revisa primero los logs de `lavalink` y `yt-cipher`. Actualiza de forma controlada sus imágenes o el plugin declarado en `lavalink/application.yml`; no agregues cookies de tu cuenta principal.
