from __future__ import annotations

import discord
import wavelink

from src.player import MusicError, MusicManager
from src.queue import LoopMode


class PlayerControls(discord.ui.View):
    def __init__(self, manager: MusicManager) -> None:
        super().__init__(timeout=900)
        self.manager = manager

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
        player = interaction.guild.voice_client
        await self.manager.pause(interaction.guild, interaction.user, not player.paused)
        await interaction.response.send_message("Pausa alternada.", ephemeral=True)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        try:
            await self.manager.previous(interaction.guild, interaction.user)
        except MusicError:
            await interaction.response.send_message("No hay una canción anterior.", ephemeral=True)
            return
        await interaction.response.send_message("Reproduciendo la canción anterior.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        await self.manager.skip(interaction.guild, interaction.user)
        await interaction.response.send_message("Canción omitida.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        await self.manager.stop(interaction.guild, interaction.user)
        await interaction.response.send_message("Reproducción y cola detenidas.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        queue = self.manager.session(interaction.guild.id).queue
        modes = [LoopMode.OFF, LoopMode.TRACK, LoopMode.QUEUE]
        await self.manager.set_loop(interaction.guild.id, modes[(modes.index(queue.loop_mode) + 1) % len(modes)])
        await interaction.response.send_message(f"Loop: `{queue.loop_mode.value}`.", ephemeral=True)

    @discord.ui.button(label="Cola", emoji="📜", style=discord.ButtonStyle.secondary)
    async def queue(self, interaction: discord.Interaction, _: discord.ui.Button[discord.ui.View]) -> None:
        items = list(self.manager.session(interaction.guild.id).queue.items)
        lines = [f"`{index}.` {track.title} — {track.author}" for index, track in enumerate(items[:10], 1)]
        await interaction.response.send_message("\n".join(lines) or "La cola está vacía.", ephemeral=True)
