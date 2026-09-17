import _init_
import discord
import asyncio
from discord.ext import commands
from creds.api_creds import TOKEN
from src.voice_channel_connection import VoiceConnection
from src.music_player import Music

# Habilitar intents necesarios
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

async def main():
    async with bot:
        # Cargar el Cog
        await bot.add_cog(VoiceConnection(bot=bot))
        await bot.add_cog(Music(bot=bot))
        await bot.start(TOKEN)

asyncio.run(main())