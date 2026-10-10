from __future__ import annotations

import logging

import discord

from src.player import MusicError, MusicManager
from src.queue import LoopMode

LOGGER = logging.getLogger(__name__)


class PlayerControls(discord.ui.View):
    def __init__(self, manager: MusicManager) -> None:
        super().__init__(timeout=900)
        self.manager = manager

    @staticmethod
    def _context(interaction: discord.Interaction) -> tuple[discord.Guild, discord.Member]:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise MusicError("Este control solo funciona dentro del servidor.")
        return interaction.guild, interaction.user

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        LOGGER.exception("player_control_failed item=%s", type(item).__name__, exc_info=error)
        message = f"⚠️ {error}" if isinstance(error, MusicError) else "⚠️ No pude ejecutar ese control."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        try:
            self.manager.require_same_channel(interaction.guild, interaction.user)
        except MusicError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        guild, member = self._context(interaction)
        player = self.manager.require_same_channel(guild, member)
        await self.manager.pause(guild, member, not player.paused)
        await interaction.response.send_message("Pausa alternada.", ephemeral=True)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        guild, member = self._context(interaction)
        try:
            await self.manager.previous(guild, member)
        except MusicError:
            await interaction.response.send_message("No hay una canción anterior.", ephemeral=True)
            return
        await interaction.response.send_message("Reproduciendo la canción anterior.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        guild, member = self._context(interaction)
        await self.manager.skip(guild, member)
        await interaction.response.send_message("Canción omitida.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(
        self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]
    ) -> None:
        guild, member = self._context(interaction)
        await self.manager.stop(guild, member)
        await interaction.response.send_message("Reproducción y cola detenidas.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        guild = self._context(interaction)[0]
        queue = self.manager.session(guild.id).queue
        modes = [LoopMode.OFF, LoopMode.TRACK, LoopMode.QUEUE]
        await self.manager.set_loop(guild.id, modes[(modes.index(queue.loop_mode) + 1) % len(modes)])
        await interaction.response.send_message(f"Loop: `{queue.loop_mode.value}`.", ephemeral=True)

    @discord.ui.button(label="Cola", emoji="📜", style=discord.ButtonStyle.secondary)
    async def queue(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        guild = self._context(interaction)[0]
        items = list(self.manager.session(guild.id).queue.items)
        lines = [f"`{index}.` {track.title} — {track.author}" for index, track in enumerate(items[:10], 1)]
        await interaction.response.send_message("\n".join(lines) or "La cola está vacía.", ephemeral=True)
