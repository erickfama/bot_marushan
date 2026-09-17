import _init_
import time
import discord
from discord.ext import commands
from discord.voice_client import VoiceClient, has_nacl


class VoiceConnection(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        # Tell the type checker that User is filled up at this point
        assert self.bot.user is not None

        print(f"Logged in as {self.bot.user} (ID: {self.bot.user.id})")
        print("------")

    @commands.command(name="join", help="Tells the bot to join the voice channel")
    async def join(self, ctx):
        if not ctx.message.author.voice:
            await ctx.send(f"{ctx.message.author.name} is not connected to a voice channel")
            return
        
        channel = ctx.message.author.voice.channel

        if ctx.voice_client is not None:
            return await ctx.voice_client.move_to(channel)
        
        started = time.time()
        try:
            vc = await channel.connect()

        except Exception as e:
            raise

        await ctx.send(f"Connected to {channel}")