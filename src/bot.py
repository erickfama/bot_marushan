import _init_
import os
import json
import time
import discord
from discord.ext import commands
import asyncio
from creds.api_creds import TOKEN
from src.voice_channel_connection import VoiceConnection


_DEBUG_LOG = os.path.join(os.path.dirname(__file__), '..', 'debug-dde48f.log')

def _agent_log(hypothesis_id, location, message, data=None):
    try:
        with open(_DEBUG_LOG, 'a', encoding='utf-8') as _f:
            _f.write(json.dumps({
                'sessionId': 'dde48f',
                'runId': 'run1',
                'hypothesisId': hypothesis_id,
                'location': location,
                'message': message,
                'data': data or {},
                'timestamp': int(time.time() * 1000),
            }) + '\n')
    except Exception:
        pass

# Habilitar intents necesarios para voz
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

async def main():
    async with bot:
        # Cargar el Cog
        await bot.add_cog(VoiceConnection(bot=bot, agent_log=_agent_log))
        await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())