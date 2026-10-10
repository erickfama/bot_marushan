from __future__ import annotations

import logging
from pathlib import Path

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from src.player import MusicError, MusicManager
from src.queue import LoopMode
from src.utils import format_time, parse_time
from src.views import PlayerControls

LOGGER = logging.getLogger(__name__)


def unwrap_command_error(error: BaseException) -> BaseException:
    """Return the original exception hidden by Discord command wrappers."""
    current = error
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        original = getattr(current, "original", None)
        if not isinstance(original, BaseException) or original is current:
            break
        current = original
    return current


def track_name(track: wavelink.Playable) -> str:
    spotify_title = getattr(track.extras, "spotify_title", None)
    spotify_artists = getattr(track.extras, "spotify_artists", None)
    return f"{spotify_title} — {spotify_artists}" if spotify_title else f"{track.title} — {track.author}"


class MusicCog(commands.Cog, name="Música"):
    def __init__(self, bot: commands.Bot, manager: MusicManager) -> None:
        self.bot = bot
        self.manager = manager

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        original = unwrap_command_error(error)
        if isinstance(original, (MusicError, ValueError, IndexError)):
            await ctx.send(f"⚠️ {original}", ephemeral=bool(ctx.interaction))
            return
        LOGGER.exception("command_failed command=%s", ctx.command, exc_info=original)
        await ctx.send("⚠️ Ocurrió un error inesperado. Revisa los logs del bot.", ephemeral=bool(ctx.interaction))

    def _member(self, ctx: commands.Context) -> discord.Member:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            raise MusicError("Este comando solo funciona dentro del servidor.")
        return ctx.author

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        if payload.player is None:
            LOGGER.warning("track_end_without_player reason=%s", payload.reason)
            return
        await self.manager.on_track_end(payload.player, payload.reason, payload.original or payload.track)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        if payload.player is None:
            return
        LOGGER.info(
            "track_start guild_id=%s title=%r identifier=%s duration=%s",
            payload.player.guild.id,
            payload.track.title,
            payload.track.identifier,
            payload.track.length,
        )

    @commands.Cog.listener()
    async def on_wavelink_player_update(self, payload: wavelink.PlayerUpdateEventPayload) -> None:
        if payload.player is not None:
            self.manager.record_player_update(payload.player, payload.position, payload.connected)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        if payload.player is None:
            LOGGER.error("track_exception_without_player error=%s", payload.exception)
            return
        LOGGER.error("track_exception guild_id=%s error=%s", payload.player.guild.id, payload.exception)
        self.manager.schedule_recovery(payload.player, payload.track, cause="track_exception")

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        if payload.player is None:
            LOGGER.error("track_stuck_without_player threshold=%s", payload.threshold)
            return
        LOGGER.error("track_stuck guild_id=%s threshold=%s", payload.player.guild.id, payload.threshold)
        self.manager.schedule_recovery(payload.player, payload.track, cause="track_stuck", delay=0)

    @commands.Cog.listener()
    async def on_wavelink_websocket_closed(self, payload: wavelink.WebsocketClosedEventPayload) -> None:
        LOGGER.warning(
            "voice_websocket_closed guild_id=%s code=%s remote=%s reason=%r",
            payload.player.guild.id if payload.player else None,
            payload.code,
            payload.by_remote,
            payload.reason,
        )

    @commands.hybrid_command(name="join", aliases=["j", "connect"], description="Conecta el bot a tu canal de voz")
    @commands.guild_only()
    async def join(self, ctx: commands.Context) -> None:
        player = await self.manager.ensure_player(ctx.guild, self._member(ctx))
        await ctx.send(f"🔊 Conectado a **{player.channel.name}**.")

    @commands.hybrid_command(name="play", aliases=["p"], description="Busca o agrega música a la cola")
    @commands.guild_only()
    @app_commands.describe(consulta="Título, enlace de YouTube/Spotify o radio", siguiente="Insertar al principio de la cola")
    async def play(self, ctx: commands.Context, *, consulta: str, siguiente: bool = False) -> None:
        LOGGER.info(
            "play_command_received interface=%s guild_id=%s user_id=%s",
            "slash" if ctx.interaction else "prefix",
            ctx.guild.id if ctx.guild else None,
            ctx.author.id,
        )
        if ctx.interaction:
            await ctx.defer()
        accepted, omitted, first = await self.manager.enqueue(
            ctx.guild, self._member(ctx), ctx.channel.id, consulta, next_up=siguiente
        )
        message = f"✅ **{track_name(first)}**"
        current = self.manager.session(ctx.guild.id).queue.current
        if accepted == 1:
            message += (
                " se está reproduciendo."
                if self.manager._same_track(current, first)
                else " fue agregada a la cola."
            )
        else:
            message += f" y **{accepted - 1}** más fueron agregadas."
        if omitted:
            message += f" No se pudieron agregar **{omitted}** pistas."
        await ctx.send(message, view=PlayerControls(self.manager))

    @commands.hybrid_command(name="play-file", description="Reproduce un archivo adjunto")
    @commands.guild_only()
    async def play_file(self, ctx: commands.Context, archivo: discord.Attachment, siguiente: bool = False) -> None:
        audio_extensions = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".webm"}
        if not (archivo.content_type or "").startswith("audio/") and Path(archivo.filename).suffix.lower() not in audio_extensions:
            raise MusicError("El archivo adjunto debe ser de audio.")
        if ctx.interaction:
            await ctx.defer()
        accepted, _, first = await self.manager.enqueue(
            ctx.guild, self._member(ctx), ctx.channel.id, archivo.url, next_up=siguiente
        )
        await ctx.send(f"✅ Archivo agregado: **{track_name(first)}** ({accepted} pista).")

    @commands.command(name="play_local", hidden=True)
    @commands.guild_only()
    async def play_local(self, ctx: commands.Context, *, ruta: str) -> None:
        media_root = Path("/media").resolve()
        candidate = (media_root / ruta).resolve()
        if media_root not in candidate.parents or not candidate.is_file():
            raise MusicError("El archivo debe existir dentro del directorio compartido `/media`.")
        accepted, _, first = await self.manager.enqueue(ctx.guild, self._member(ctx), ctx.channel.id, str(candidate))
        await ctx.send(f"✅ Archivo local agregado: **{track_name(first)}** ({accepted} pista).")

    @commands.hybrid_command(name="queue", aliases=["q"], description="Muestra la cola actual")
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context, pagina: int = 1) -> None:
        session = self.manager.session(ctx.guild.id)
        items = list(session.queue.items)
        pages = max(1, (len(items) + 9) // 10)
        if pagina < 1 or pagina > pages:
            raise MusicError(f"La página debe estar entre 1 y {pages}.")
        start = (pagina - 1) * 10
        lines = [f"`{index}.` {track_name(track)} · {format_time(track.length)}" for index, track in enumerate(items[start:start + 10], start + 1)]
        current = track_name(session.queue.current) if session.queue.current else "Nada"
        embed = discord.Embed(title="Cola musical", description="\n".join(lines) or "La cola está vacía.", color=discord.Color.blurple())
        embed.add_field(name="Reproduciendo", value=current, inline=False)
        total_duration = sum(track.length for track in items if not track.is_stream)
        embed.set_footer(text=f"Página {pagina}/{pages} · {len(items)} pendientes · {format_time(total_duration)} · Loop {session.queue.loop_mode.value}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="nowplaying", aliases=["np"], description="Muestra la canción actual")
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context) -> None:
        player = ctx.guild.voice_client
        session = self.manager.session(ctx.guild.id)
        track = session.queue.current
        if not isinstance(player, wavelink.Player) or not track:
            raise MusicError("No hay música reproduciéndose.")
        embed = discord.Embed(title="Reproduciendo ahora", description=f"**{track_name(track)}**", url=getattr(track.extras, "spotify_url", None) or track.uri, color=discord.Color.green())
        embed.add_field(name="Progreso", value=f"{format_time(player.position)} / {format_time(track.length)}")
        embed.add_field(name="Volumen", value=f"{session.volume}%")
        embed.add_field(name="Solicitada por", value=getattr(track.extras, "requester_name", "Desconocido"))
        artwork = getattr(track.extras, "spotify_artwork", None) or track.artwork
        if artwork:
            embed.set_thumbnail(url=artwork)
        if getattr(track.extras, "spotify_url", None):
            embed.set_footer(text="Metadatos de Spotify · Audio resuelto en YouTube")
        await ctx.send(embed=embed, view=PlayerControls(self.manager))

    @commands.hybrid_command(name="pause", description="Pausa la reproducción")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context) -> None:
        await self.manager.pause(ctx.guild, self._member(ctx), True)
        await ctx.send("⏸️ Reproducción pausada.")

    @commands.hybrid_command(name="resume", aliases=["unpause"], description="Continúa la reproducción")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context) -> None:
        await self.manager.pause(ctx.guild, self._member(ctx), False)
        await ctx.send("▶️ Reproducción reanudada.")

    @commands.hybrid_command(name="skip", description="Omite la canción actual")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context) -> None:
        await self.manager.skip(ctx.guild, self._member(ctx))
        await ctx.send("⏭️ Canción omitida.")

    @commands.hybrid_command(name="previous", aliases=["prev"], description="Regresa a la canción anterior")
    @commands.guild_only()
    async def previous(self, ctx: commands.Context) -> None:
        track = await self.manager.previous(ctx.guild, self._member(ctx))
        await ctx.send(f"⏮️ Reproduciendo **{track_name(track)}**.")

    @commands.hybrid_command(name="stop", description="Detiene la música y limpia la cola")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context) -> None:
        await self.manager.stop(ctx.guild, self._member(ctx))
        await ctx.send("⏹️ Reproducción detenida y cola limpiada.")

    @commands.hybrid_command(name="disconnect", aliases=["dc", "leave"], description="Desconecta el bot")
    @commands.guild_only()
    async def disconnect(self, ctx: commands.Context) -> None:
        await self.manager.disconnect(ctx.guild, self._member(ctx))
        await ctx.send("👋 Desconectado del canal de voz.")

    @commands.hybrid_command(name="remove", aliases=["rm"], description="Elimina una canción de la cola")
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, posicion: int) -> None:
        self.manager.require_same_channel(ctx.guild, self._member(ctx))
        track = await self.manager.remove(ctx.guild.id, posicion)
        await ctx.send(f"🗑️ Eliminé **{track_name(track)}**.")

    @commands.hybrid_command(name="move", description="Mueve una canción dentro de la cola")
    @commands.guild_only()
    async def move(self, ctx: commands.Context, origen: int, destino: int) -> None:
        self.manager.require_same_channel(ctx.guild, self._member(ctx))
        await self.manager.move(ctx.guild.id, origen, destino)
        await ctx.send(f"↕️ Moví la canción {origen} a la posición {destino}.")

    @commands.hybrid_command(name="clear", aliases=["clearqueue", "cq"], description="Vacía las canciones pendientes")
    @commands.guild_only()
    async def clear(self, ctx: commands.Context) -> None:
        self.manager.require_same_channel(ctx.guild, self._member(ctx))
        count = await self.manager.clear(ctx.guild.id)
        await ctx.send(f"🧹 Eliminé **{count}** canciones pendientes.")

    @commands.hybrid_command(name="shuffle", description="Mezcla la cola")
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context) -> None:
        self.manager.require_same_channel(ctx.guild, self._member(ctx))
        await self.manager.shuffle(ctx.guild.id)
        await ctx.send("🔀 Cola mezclada.")

    @commands.hybrid_command(name="jump", description="Salta a una posición de la cola")
    @commands.guild_only()
    async def jump(self, ctx: commands.Context, posicion: int) -> None:
        await self.manager.jump(ctx.guild, self._member(ctx), posicion)
        await ctx.send(f"⏩ Saltando a la posición {posicion}.")

    @commands.hybrid_command(name="seek", description="Busca un punto de la canción")
    @commands.guild_only()
    async def seek(self, ctx: commands.Context, tiempo: str) -> None:
        track = self.manager.session(ctx.guild.id).queue.current
        if not track or track.is_stream:
            raise MusicError("No se puede buscar dentro de un stream en vivo.")
        milliseconds = parse_time(tiempo) * 1000
        if milliseconds >= track.length:
            raise MusicError(f"La canción termina en {format_time(track.length)}.")
        await self.manager.seek(ctx.guild, self._member(ctx), milliseconds)
        await ctx.send(f"⏩ Posición: **{format_time(milliseconds)}**.")

    @commands.hybrid_command(name="volume", aliases=["vol"], description="Consulta o cambia el volumen")
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, volumen: int | None = None) -> None:
        self.manager.require_same_channel(ctx.guild, self._member(ctx))
        session = self.manager.session(ctx.guild.id)
        if volumen is None:
            await ctx.send(f"🔊 Volumen actual: **{session.volume}%**.")
            return
        await self.manager.set_volume(ctx.guild, self._member(ctx), volumen)
        await ctx.send(f"🔊 Volumen cambiado a **{volumen}%**.")

    @commands.hybrid_command(name="loop", description="Configura repetición de canción o cola")
    @commands.guild_only()
    @app_commands.choices(modo=[
        app_commands.Choice(name="Desactivado", value="off"),
        app_commands.Choice(name="Canción", value="track"),
        app_commands.Choice(name="Cola", value="queue"),
    ])
    async def loop(self, ctx: commands.Context, modo: str) -> None:
        self.manager.require_same_channel(ctx.guild, self._member(ctx))
        await self.manager.set_loop(ctx.guild.id, LoopMode(modo))
        await ctx.send(f"🔁 Loop configurado en `{modo}`.")

    @commands.hybrid_command(name="autoplay", description="Activa o desactiva recomendaciones automáticas")
    @commands.guild_only()
    async def autoplay(self, ctx: commands.Context, activo: bool) -> None:
        self.manager.require_same_channel(ctx.guild, self._member(ctx))
        await self.manager.set_autoplay(ctx.guild.id, activo)
        await ctx.send(f"♾️ Autoplay **{'activado' if activo else 'desactivado'}**.")
