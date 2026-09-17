import discord
import asyncio
import yt_dlp
import static_ffmpeg
from discord.ext import commands

# Agrega ffmpeg y ffprobe al PATH del entorno en tiempo de ejecución
static_ffmpeg.add_paths()

# Opciones para que FFmpeg lea directamente de la URL de streaming sin trabarse
ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": False,
    "verbose": True,
    "no_warnings": False,
    "default_search": "auto",
    "source_address": "0.0.0.0",  # bind to ipv4 since ipv6 addresses cause issues sometimes
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)

        self.data = data

        self.title = data.get("title")
        self.url = data.get("url")

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        print("[from_url] start")
        loop = loop or asyncio.get_event_loop()
        print("[from_url] start1")
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

        print("[from_url] start2")
        if "entries" in data:
            # take first item from a playlist
            data = data["entries"][0]

        print("[from_url] start3")
        # Si stream es True, filename es la URL directa de audio de GoogleVideo
        filename = data["url"] if stream else ytdl.prepare_filename(data)

        print("[from_url] end")
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)
        

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def play_local(self, ctx, *, query):
        """Plays a file from the local filesystem"""

        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(query))
        ctx.voice_client.play(source, after=lambda e: print(f"Player error: {e}") if e else None)

        await ctx.send(f"Now playing: {query}")

    @commands.command()
    async def play(self, ctx, *, url):
        """Plays from a url (almost anything yt_dlp supports)"""

        print(f"[yt] Tryng to play: {url}...")

        async with ctx.typing():
            player = await YTDLSource.from_url(url, loop=self.bot.loop)
            ctx.voice_client.play(player, after=lambda e: print(f"Player error: {e}") if e else None)

        await ctx.send(f"Now playing: {player.title}")

    @commands.command()
    async def volume(self, ctx, volume: int):
        """Changes the player"s volume"""

        if ctx.voice_client is None:
            return await ctx.send("Not connected to a voice channel.")

        ctx.voice_client.source.volume = volume / 100
        await ctx.send(f"Changed volume to {volume}%")

    @commands.command()
    async def stop(self, ctx):
        """Stops and disconnects the bot from voice"""

        await ctx.voice_client.disconnect()

    @play_local.before_invoke
    @play.before_invoke
    async def ensure_voice(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("You are not connected to a voice channel.")
                raise commands.CommandError("Author not connected to a voice channel.")
        elif ctx.voice_client.is_playing():
            ctx.voice_client.stop()