import asyncio
import logging

import discord

from config import CONFIG, is_allowed_guild
import db
from commands import HANDLERS, has_access, err

log = logging.getLogger('antipuzobot')


async def send_log(bot, text: str):
    ch_id = CONFIG.get('logChannelId')
    if ch_id:
        try:
            ch = bot.get_channel(int(ch_id))
            if ch and isinstance(ch, discord.abc.Messageable):
                await ch.send(text)
                return
        except (ValueError, TypeError, discord.HTTPException):
            pass
    log.info('LOG %s', text)


def check_expired_bans(bot):
    import time
    for row in db.expired_abans():
        db.deactivate_ban(row['user_id'], 'aban')  # сначала гасим в БД, чтобы не сработал авто-ребан
        for guild in bot.guilds:
            if not is_allowed_guild(guild.id):
                continue
            bot.loop.create_task(_unban_if_banned(guild, int(row['user_id']), 'истёк срок аба'))
        bot.loop.create_task(send_log(bot, f'⟨ ⏳ ⟩ Срок аба истёк: <@{row["user_id"]}> — разбанен.'))


async def _unban_if_banned(guild: discord.Guild, user_id: int, reason: str):
    try:
        await guild.fetch_ban(discord.Object(id=user_id))
        await guild.unban(discord.Object(id=user_id), reason=reason)
    except discord.NotFound:
        pass
    except discord.HTTPException:
        pass


async def on_member_join(bot, member: discord.Member):
    if not is_allowed_guild(member.guild.id):
        return
    rape = db.get_active_ban(member.id, 'rape')
    if rape:
        await member.ban(reason=f"[RAPE] авто-ребан при заходе | причина: {rape['reason'] or '—'}")
        await send_log(bot, f'⟨ ✔ ⟩ RAPE-ребан: <@{member.id}> забанен при попытке зайти.')
        return

    aban = db.get_active_ban(member.id, 'aban')
    if not aban:
        return

    import time
    if time.time() * 1000 < aban['expires_at']:
        left = max(1, round((aban['expires_at'] - time.time() * 1000) / 86_400_000))
        await member.ban(reason=f'[ABAN] авто-ребан при заходе | осталось ~{left} дн.')
        await send_log(bot, f'⟨ ✔ ⟩ ABAN-ребан: <@{member.id}> забанен при попытке зайти (осталось ~{left} дн.).')
    else:
        db.deactivate_ban(member.id, 'aban')


async def on_ban_remove(bot, ban: discord.guild.Ban):
    # Ручной разбан аба => мгновенный ребан ("если разбанить — сразу банит автоматически")
    if not is_allowed_guild(ban.guild.id):
        return
    row = db.get_active_ban(ban.user.id, 'aban')
    if not row:
        return

    import time
    if time.time() * 1000 >= row['expires_at']:
        db.deactivate_ban(ban.user.id, 'aban')
        return

    async def reban():
        await asyncio.sleep(1.5)
        try:
            await ban.guild.ban(discord.Object(id=ban.user.id), reason='[ABAN] авто-ребан после разбана')
            await send_log(bot, f'⟨ ✔ ⟩ ABAN-ребан: <@{ban.user.id}> разбанили — забанен обратно.')
        except discord.HTTPException:
            pass

    bot.loop.create_task(reban())


async def on_member_update(before: discord.Member, after: discord.Member):
    # Прота (.dope): сохранённая роль возвращается, если её сняли
    if not is_allowed_guild(after.guild.id):
        return
    if not db.get_dope(after.id):
        return

    saved = db.get_saved_role(after.id)
    if not saved:
        return

    role = after.guild.get_role(int(saved['role_id']))
    if role is None:
        db.del_saved_role(after.id)  # роль удалили с сервера
        return

    before_ids = {r.id for r in before.roles}
    after_ids = {r.id for r in after.roles}
    if role.id in before_ids and role.id not in after_ids:
        try:
            await after.add_roles(role, reason='восстановление сохранённой роли (.dope)')
            await send_log(after.client, f'⟨ ✔ ⟩ Восстановлена роль <@&{role.id}> для <@{after.id}>.')
        except discord.HTTPException:
            pass


async def on_message(bot, msg: discord.Message):
    if msg.author.bot or msg.guild is None:
        return
    if not is_allowed_guild(msg.guild.id):
        return

    prefix = CONFIG.get('prefix', '.')
    if not msg.content.startswith(prefix):
        return

    parts = [p for p in msg.content[len(prefix):].split() if p]
    if not parts:
        return
    name = parts[0].lower()
    args = parts[1:]

    handler = HANDLERS.get(name)
    if handler is None:
        return

    if not has_access(msg.author, name):
        await msg.channel.send(embed=err('Недостаточно прав для этой команды.'))
        return

    try:
        reply = await handler(msg, args)
        if reply is not None:
            await msg.channel.send(embed=reply)
    except Exception:
        log.exception('[CMD:%s]', name)
        try:
            await msg.channel.send(embed=err('Ошибка выполнения команды.'))
        except discord.HTTPException:
            pass


async def _expired_loop(bot):
    while True:
        try:
            check_expired_bans(bot)
        except Exception:
            log.exception('[scheduler]')
        await asyncio.sleep(60)


async def register(bot):
    # имена обёрток НЕ должны совпадать с модульными функциями (иначе рекурсия)
    async def _on_message(msg: discord.Message):
        await on_message(bot, msg)

    async def _on_command_error(ctx, error):
        pass  # команды у нас обрабатываются вручную, шум discord.py не нужен

    async def _on_member_join(member: discord.Member):
        try:
            await on_member_join(bot, member)
        except Exception:
            log.exception('[guildMemberAdd]')

    async def _on_ban_remove(ban: discord.guild.Ban):
        try:
            await on_ban_remove(bot, ban)
        except Exception:
            log.exception('[guildBanRemove]')

    async def _on_member_update(before: discord.Member, after: discord.Member):
        try:
            await on_member_update(before, after)
        except Exception:
            log.exception('[guildMemberUpdate]')

    bot.add_listener(_on_message, 'on_message')
    bot.add_listener(_on_command_error, 'on_command_error')
    bot.add_listener(_on_member_join, 'on_member_join')
    bot.add_listener(_on_ban_remove, 'on_ban_remove')
    bot.add_listener(_on_member_update, 'on_member_update')

    bot.loop.create_task(_expired_loop(bot))
