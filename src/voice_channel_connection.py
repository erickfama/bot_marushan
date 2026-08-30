import _init_
import time
import discord
from discord.ext import commands
from discord.voice_client import VoiceClient, has_nacl


class VoiceConnection(commands.Cog):
    def __init__(self, bot, agent_log):
        self.bot = bot
        self.agent_log = agent_log

    @commands.Cog.listener()
    async def on_ready(self):
        print(f'Logged in as {self.bot.user.name}')
        # #region agent log
        self.agent_log('A', 'bot.py:on_ready', 'bot ready voice stack', {
            'discord_version': discord.__version__,
            'has_nacl': bool(has_nacl),
            'supported_modes': list(VoiceClient.supported_modes),
            'voice_states_intent': bool(self.bot.intents.voice_states),
            'opus_loaded': bool(discord.opus.is_loaded()),
        })
        # #endregion

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if self.bot.user and member.id == self.bot.user.id:
            # #region agent log
            self.agent_log('D', 'bot.py:on_voice_state_update', 'bot voice state changed', {
                'before_channel': getattr(before.channel, 'id', None),
                'after_channel': getattr(after.channel, 'id', None),
                'connected': bool(after.channel),
            })
            # #endregion

    @commands.command(name='join', help='Tells the bot to join the voice channel')
    async def join(self, ctx):
        if not ctx.message.author.voice:
            await ctx.send(f"{ctx.message.author.name} is not connected to a voice channel")
            return
        
        channel = ctx.message.author.voice.channel
        # #region agent log
        self.agent_log('C', 'bot.py:join:entry', 'join invoked', {
            'channel_id': getattr(channel, 'id', None),
            'existing_voice_client': ctx.voice_client is not None,
            'existing_connected': bool(ctx.voice_client and ctx.voice_client.is_connected()),
            'voice_states_intent': bool(self.bot.intents.voice_states),
        })
        # #endregion

        if ctx.voice_client is not None:
            return await ctx.voice_client.move_to(channel)
        
        started = time.time()
        try:
            vc = await channel.connect()
            # #region agent log
            self.agent_log('A', 'bot.py:join:after_connect', 'connect returned', {
                'elapsed_s': round(time.time() - started, 3),
                'is_connected': bool(vc and vc.is_connected()),
                'mode': getattr(vc, 'mode', None),
                'endpoint': getattr(vc, 'endpoint', None),
                'has_nacl': bool(has_nacl),
            })
            # #endregion
        except Exception as e:
            # #region agent log
            self.agent_log('E', 'bot.py:join:connect_error', 'connect raised', {
                'elapsed_s': round(time.time() - started, 3),
                'exc_type': type(e).__name__,
                'exc_msg': str(e)[:300],
            })
            # #endregion
            raise

        await ctx.send(f"Connected to {channel}")
        # #region agent log
        self.agent_log('D', 'bot.py:join:after_send', 'join finished', {
            'still_connected': bool(ctx.voice_client and ctx.voice_client.is_connected()),
        })
        # #endregion