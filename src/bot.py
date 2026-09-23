from __future__ import annotations

import asyncio
import logging

import discord
import wavelink
from discord.ext import commands

from src.config import Settings
from src.music_cog import MusicCog
from src.player import MusicManager
from src.spotify import SpotifyClient


class MarushanBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents, help_command=commands.DefaultHelpCommand(dm_help=False))
        self.settings = settings
        spotify = None
        if settings.spotify_enabled:
            spotify = SpotifyClient(
                settings.spotify_client_id or "",
                settings.spotify_client_secret or "",
                settings.spotify_refresh_token or "",
            )
        self.music = MusicManager(self, settings, spotify)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is discord.InteractionType.application_command:
            command_name = interaction.data.get("name") if interaction.data else None
            logging.getLogger(__name__).info(
                "slash_interaction_received command=%s guild_id=%s user_id=%s",
                command_name,
                interaction.guild_id,
                interaction.user.id,
            )
        await super().on_interaction(interaction)

    async def setup_hook(self) -> None:
        node = wavelink.Node(
            uri=self.settings.lavalink_uri,
            password=self.settings.lavalink_password,
            identifier="marushan-main",
            retries=10,
            inactive_player_timeout=self.settings.idle_timeout_seconds,
        )
        await wavelink.Pool.connect(nodes=[node], client=self, cache_capacity=100)
        await self.add_cog(MusicCog(self, self.music))
        guild = discord.Object(id=self.settings.discord_guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def close(self) -> None:
        await self.music.close()
        await super().close()

    async def on_ready(self) -> None:
        await self._apply_display_name()
        logging.getLogger(__name__).info("bot_ready user=%s guild_id=%s", self.user, self.settings.discord_guild_id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._set_guild_nickname(guild)

    async def _apply_display_name(self) -> None:
        for guild in self.guilds:
            await self._set_guild_nickname(guild)

    async def _set_guild_nickname(self, guild: discord.Guild) -> None:
        member = guild.me
        if not member or member.nick == self.settings.bot_display_name:
            return
        try:
            await member.edit(nick=self.settings.bot_display_name, reason="Nombre configurado para el bot musical")
        except discord.Forbidden:
            logging.getLogger(__name__).warning("nickname_permission_missing guild_id=%s", guild.id)
        except discord.HTTPException:
            logging.getLogger(__name__).exception("nickname_update_failed guild_id=%s", guild.id)


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    async with MarushanBot(settings) as bot:
        await bot.start(settings.discord_token, reconnect=True)


if __name__ == "__main__":
    asyncio.run(main())
