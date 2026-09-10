import asyncio
import logging
import sys
import threading

import discord
from discord.ext import commands

from config import CONFIG
import events
import antinuke

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('antipuzobot')

# ===================== АНТИКРАШ =====================
# Ловим всё, что может уронить процесс: необработанные исключения
# в потоках, asyncio-задачах и ошибка клиента discord.py.


def _sys_hook(tp, val, tb):
    log.critical('[ANTICRASH] uncaughtException:', exc_info=(tp, val, tb))


def _thread_hook(args: threading.ExceptHookArgs):
    log.critical(
        '[ANTICRASH] exception in thread %s:',
        args.thread.name if args.thread else '?',
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _loop_handler(loop, context):
    log.error('[ANTICRASH] asyncio task error: %s', context.get('message'),
              exc_info=context.get('exception'))


class AntiPuzoBot(commands.Bot):
    async def setup_hook(self):
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_loop_handler)
        await events.register(self)
        await antinuke.register(self)


intents = discord.Intents.all()

bot = AntiPuzoBot(command_prefix=CONFIG['prefix'], intents=intents, help_command=None)


@bot.event
async def on_ready():
    log.info('[READY] %s запущен. Серверов: %d', bot.user, len(bot.guilds))


if __name__ == '__main__':
    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    try:
        bot.run(CONFIG['token'], log_handler=None)
    except discord.LoginFailure:
        log.error('[LOGIN] Неверный токен.')
    except discord.PrivilegedIntentsRequired:
        log.error(
            '[INTENTS] Включи SERVER MEMBERS INTENT и MESSAGE CONTENT INTENT '
            'в Discord Developer Portal (Bot -> Privileged Gateway Intents).'
        )
    except KeyboardInterrupt:
        pass
