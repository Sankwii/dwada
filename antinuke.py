"""Антикраш-система: лимиты по категориям, вайтлист с категориями,
восстановление каналов/категорий/ролей с правами, защита от ботов."""

import asyncio
import json
import logging
import time

import discord

from config import CONFIG, is_allowed_guild
import db
from events import send_log

log = logging.getLogger('antipuzobot.antinuke')

BOT = None  # ссылка на client, выставляется в register()
CATEGORY_MAP = {}  # old_category_id -> new_category_id (при пересоздании категорий)


def _cfg() -> dict:
    return CONFIG.get('antinuke') or {}


def enabled() -> bool:
    return bool(_cfg().get('enabled', True))


def _guild_ok(obj) -> bool:
    """Проверка: событие пришло с разрешённого сервера (guildId в конфиге)."""
    g = getattr(obj, 'guild', None)
    if g is None and hasattr(obj, 'id') and hasattr(obj, 'name'):
        g = obj  # сам Guild
    if g is None or not hasattr(g, 'id'):
        return True
    return is_allowed_guild(g.id)


# --------------------------- лимиты/счётчики ---------------------------

def get_limit(category: str) -> int:
    n = db.get_limit(category)
    if n is not None:
        return n
    return int(_cfg().get('defaultLimit', 1))


# ------------------------------ аудит ------------------------------

async def find_actor(guild: discord.Guild, action, target_id=None, max_age=10.0):
    """Кто сделал действие (по аудиту). None — не удалось определить."""
    for attempt in range(2):
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                if (discord.utils.utcnow() - entry.created_at).total_seconds() > max_age:
                    continue
                if target_id is not None:
                    tid = getattr(entry.target, 'id', entry.target)
                    if tid != target_id:
                        continue
                return entry.user
            return None
        except discord.Forbidden:
            log.warning('[ANTINUKE] нет права View Audit Log на %s', guild.name)
            return None
        except discord.HTTPException as e:
            if attempt == 0:
                await asyncio.sleep(1.5)  # скорее всего рейтлимит аудита — пробуем ещё раз
                continue
            log.warning('[ANTINUKE] аудит недоступен: %s', e)
            return None
    return None


async def judge(guild: discord.Guild, category: str, actor):
    """Возвращает: 'exempt' | 'protected' | 'unknown' | 'ok' | 'punished'."""
    if actor is None:
        return 'unknown'
    if BOT and actor.id == BOT.user.id:
        return 'exempt'
    if actor.id == guild.owner_id:
        return 'exempt'
    oid = CONFIG.get('ownerId')
    if oid and str(actor.id) == str(oid):
        return 'exempt'  # владелец бота не может быть забанен антикрашем
    if db.wl_protected(actor.id, category):
        return 'protected'

    cnt = db.bump_counter(guild.id, actor.id, category)
    lim = get_limit(category)
    if cnt < lim:
        return 'ok'

    db.reset_counter(guild.id, actor.id, category)
    reason = f"[ANTINUKE] превышен лимит '{category}' ({cnt}/{lim})"
    try:
        await guild.ban(actor, reason=reason, delete_message_seconds=0)
        await send_log(
            BOT,
            f"⟨ 🔨 ⟩ **АНТИКРАШ**: <@{actor.id}> (`{actor.id}`) забанен — "
            f"`{category}` ({cnt}/{lim}).",
        )
    except discord.Forbidden:
        log.warning('[ANTINUKE] не могу забанить %s (нет прав/роль выше)', actor)
    except discord.HTTPException as e:
        log.warning('[ANTINUKE] бан не удался: %s', e)
    return 'punished'


# --------------------------- сериализация ---------------------------

def serialize_channel(c) -> dict:
    is_cat = isinstance(c, discord.CategoryChannel)
    overwrites = []
    for target, ow in c.overwrites.items():
        allow, deny = ow.pair()
        overwrites.append({
            'id': str(target.id),
            'allow': allow.value,
            'deny': deny.value,
        })
    parent_id = str(c.category_id) if getattr(c, 'category_id', None) else None
    if is_cat:
        parent_id = None
    return {
        'channel_id': str(c.id),
        'guild_id': str(c.guild.id),
        'kind': 'category' if is_cat else 'channel',
        'name': c.name,
        'type': int(c.type.value) if not is_cat else 4,
        'position': c.position,
        'parent_id': parent_id,
        'topic': getattr(c, 'topic', None),
        'nsfw': int(bool(getattr(c, 'is_nsfw', lambda: False)())),
        'bitrate': getattr(c, 'bitrate', None),
        'user_limit': getattr(c, 'user_limit', None),
        'rate_limit': getattr(c, 'slowmode_delay', None),
        'overwrites': json.dumps(overwrites),
    }


def serialize_role(r: discord.Role) -> dict:
    return {
        'role_id': str(r.id),
        'guild_id': str(r.guild.id),
        'name': r.name,
        'color': r.colour.value,
        'permissions': r.permissions.value,
        'hoist': int(r.hoist),
        'mentionable': int(r.mentionable),
        'position': r.position,
    }


def build_overwrites(guild: discord.Guild, raw_json: str) -> dict | None:
    try:
        items = json.loads(raw_json or '[]')
    except ValueError:
        return None
    out = {}
    for it in items:
        tid = int(it['id'])
        target = guild.get_role(tid) or guild.get_member(tid)
        if target is None:
            continue
        out[target] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(int(it['allow'])),
            discord.Permissions(int(it['deny'])),
        )
    return out or None


# --------------------------- восстановление ---------------------------

async def _restore_channel_row(guild: discord.Guild, row) -> discord.abc.GuildChannel | None:
    overwrites = build_overwrites(guild, row['overwrites'])
    ctype = int(row['type'] or 0)
    kw = {'name': row['name'], 'reason': '[ANTINUKE] восстановление после удаления'}
    if overwrites:
        kw['overwrites'] = overwrites

    # возвращаем канал в его категорию (даже если категорию пересоздали — см. CATEGORY_MAP)
    if row['kind'] != 'category' and row['parent_id']:
        pid = CATEGORY_MAP.get(str(row['parent_id']), str(row['parent_id']))
        parent = guild.get_channel(int(pid))
        if parent is not None:
            kw['category'] = parent

    try:
        if row['kind'] == 'category':
            new = await guild.create_category(**kw)
        elif ctype in (0, 5):
            new = await guild.create_text_channel(
                topic=row['topic'],
                nsfw=bool(row['nsfw']),
                slowmode_delay=int(row['rate_limit'] or 0),
                news=(ctype == 5),
                **kw,
            )
        elif ctype == 2:
            new = await guild.create_voice_channel(
                bitrate=int(row['bitrate'] or 64000),
                user_limit=int(row['user_limit'] or 0),
                **kw,
            )
        elif ctype == 13:
            new = await guild.create_stage_channel(**kw)
        elif ctype == 15:
            new = await guild.create_forum(nsfw=bool(row['nsfw']), **kw)
        else:
            new = await guild.create_text_channel(**kw)

        try:
            await new.edit(position=int(row['position'] or 0))
        except discord.HTTPException:
            pass
        return new
    except discord.HTTPException as e:
        log.error('[ANTINUKE] не удалось восстановить канал %s: %s', row['name'], e)
        return None


async def restore_channel(guild: discord.Guild, row) -> bool:
    new = await _restore_channel_row(guild, row)
    if new is None:
        return False
    db.rekey_backup_channel(row['channel_id'], new.id)
    db.db.commit()
    return True


async def restore_category_and_children(guild: discord.Guild, cat_row) -> bool:
    old_cat_id = cat_row['channel_id']
    newcat = await _restore_channel_row(guild, cat_row)
    if newcat is None:
        return False

    db.rekey_backup_channel(old_cat_id, newcat.id)
    CATEGORY_MAP[old_cat_id] = str(newcat.id)

    # все дочерние каналы в бэкапе теперь привязаны к новой категории
    children = db.backup_children(old_cat_id)
    db.db.execute(
        'UPDATE backup_channels SET parent_id = ? WHERE parent_id = ?',
        (str(newcat.id), old_cat_id),
    )
    db.db.commit()

    for child in children:
        real = guild.get_channel(int(child['channel_id']))
        if real is None:
            continue
        try:
            await real.edit(category=newcat)
            await real.edit(position=int(child['position'] or 0))
        except discord.HTTPException:
            pass
    return True


async def restore_role(guild: discord.Guild, row) -> bool:
    try:
        new_role = await guild.create_role(
            name=row['name'],
            colour=discord.Colour(int(row['color'] or 0)),
            permissions=discord.Permissions(int(row['permissions'] or 0)),
            hoist=bool(row['hoist']),
            mentionable=bool(row['mentionable']),
            reason='[ANTINUKE] восстановление после удаления',
        )
        try:
            await guild.edit_role_positions({new_role: int(row['position'] or 1)})
        except discord.HTTPException:
            pass
        db.rekey_backup_role(row['role_id'], new_role.id)
        db.db.commit()
        return True
    except discord.HTTPException as e:
        log.error('[ANTINUKE] не удалось восстановить роль %s: %s', row['name'], e)
        return False


# ----------------------------- слушатели -----------------------------

async def on_guild_channel_create(channel):
    if not enabled() or not _guild_ok(channel):
        return
    db.upsert_backup_channel(serialize_channel(channel))


def _channel_sig(c) -> tuple:
    # позицию и категорию НЕ трекаем: перемещение каналов — легитимное действие
    ow = sorted(
        (t.id, allow.value, deny.value)
        for t, o in c.overwrites.items()
        for allow, deny in (o.pair(),)
    )
    return (
        c.name, tuple(ow),
        getattr(c, 'topic', None), getattr(c, 'is_nsfw', lambda: False)(),
        getattr(c, 'slowmode_delay', None), getattr(c, 'bitrate', None),
        getattr(c, 'user_limit', None),
    )


async def on_guild_channel_update(before, after):
    if not enabled() or not _guild_ok(after):
        return
    if _channel_sig(before) == _channel_sig(after):
        return

    guild = after.guild
    actor = await find_actor(guild, discord.AuditLogAction.channel_update, after.id)
    state = await judge(guild, 'channelupdate', actor)

    if state in ('protected', 'exempt'):
        db.upsert_backup_channel(serialize_channel(after))  # легитимное изменение
        return
    if state == 'punished':  # при unknown не откатываем — вдруг менял вайтлистнутый
        try:
            await after.edit(
                name=before.name,
                topic=getattr(before, 'topic', None),
                nsfw=before.is_nsfw() if hasattr(before, 'is_nsfw') else False,
                slowmode_delay=getattr(before, 'slowmode_delay', 0),
                bitrate=getattr(before, 'bitrate', None),
                user_limit=getattr(before, 'user_limit', None),
                overwrites=dict(before.overwrites) or None,
                reason='[ANTINUKE] откат изменений канала',
            )
            await send_log(BOT, f'⟨ ↩️ ⟩ Откатил изменения канала <#{after.id}>.')
        except discord.HTTPException as e:
            log.warning('[ANTINUKE] откат канала не удался: %s', e)


async def on_guild_channel_delete(channel):
    if not enabled() or not _guild_ok(channel):
        return
    guild = channel.guild
    row = db.get_backup_channel(channel.id)
    actor = await find_actor(guild, discord.AuditLogAction.channel_delete, channel.id)
    state = await judge(guild, 'channeldelete', actor)

    if state in ('protected', 'exempt'):
        # легитимное удаление (вайтлист/владелец/бот) — убираем из бэкапа
        if row:
            db.del_backup_channel(channel.id)
            db.db.commit()
        return
    if state == 'unknown':
        # не смогли определить виновного — ничего не трогаем
        return

    if row is None:
        # снапшота не было — собираем данные из самого удалённого канала (кэш события)
        try:
            row = serialize_channel(channel)
        except Exception:
            row = None

    if row is None:
        await send_log(BOT, f"⟨ ⚠️ ⟩ Не смог восстановить канал **{getattr(channel, 'name', '?')}**: нет данных.")
        return

    kind_text = 'категорию' if row['kind'] == 'category' else 'канал'
    try:
        if row['kind'] == 'category':
            done = await restore_category_and_children(guild, row)
        else:
            done = await restore_channel(guild, row)
    except Exception:
        log.exception('[ANTINUKE] ошибка восстановления')
        done = False

    if done:
        await send_log(BOT, f'⟨ ♻️ ⟩ Восстановил {kind_text} **{row["name"]}**.')
    else:
        await send_log(BOT, f'⟨ ⚠️ ⟩ НЕ смог восстановить {kind_text} **{row["name"]}** — '
                            f'проверь права бота (Manage Channels / Manage Roles).')


_ROLE_SIG = lambda r: (r.name, r.colour.value, r.permissions.value, r.hoist, r.mentionable)

# ------------------------------ вебхуки ------------------------------

_WEBHOOK_CATS = {
    discord.AuditLogAction.webhook_create: 'webhookcreate',
    discord.AuditLogAction.webhook_update: 'webhookupdate',
    discord.AuditLogAction.webhook_delete: 'webhookdelete',
}
_seen_webhook_entries = set()  # id обработанных записей аудита (чтобы не дублировать)


async def on_webhooks_update(channel):
    """Discord шлёт одно событие на любые изменения вебхуков в канале.
    Конкретное действие выясняем по свежим записям аудита."""
    if not enabled() or not _guild_ok(channel):
        return
    guild = channel.guild

    entry = None
    now = discord.utils.utcnow()
    try:
        async for e in guild.audit_logs(limit=10):
            if (now - e.created_at).total_seconds() > 6:
                break
            if e.action in _WEBHOOK_CATS and e.id not in _seen_webhook_entries:
                entry = e
                break
    except (discord.Forbidden, discord.HTTPException) as e_:
        log.warning('[ANTINUKE] аудит вебхуков недоступен: %s', e_)
        return

    if entry is None:
        return
    _seen_webhook_entries.add(entry.id)
    if len(_seen_webhook_entries) > 500:
        _seen_webhook_entries.clear()

    category = _WEBHOOK_CATS[entry.action]
    actor = entry.user
    state = await judge(guild, category, actor)

    # несанкционированно созданный вебхук — удаляем
    if category == 'webhookcreate' and state in ('punished', 'ok'):
        wid = getattr(entry.target, 'id', entry.target)
        try:
            for wh in await guild.webhooks():
                if wh.id == wid:
                    await wh.delete(reason='[ANTINUKE] вебхук без вайтлиста')
                    await send_log(BOT, f'⟨ 🕸️ ⟩ Удалил вебхук **{wh.name}** (<@{actor.id}>).')
                    break
        except discord.HTTPException as e_:
            log.warning('[ANTINUKE] не смог удалить вебхук: %s', e_)


async def on_guild_role_create(role):
    if not enabled() or not _guild_ok(role) or role.managed:
        return  # роли ботов/интеграций не бэкапим
    db.upsert_backup_role(serialize_role(role))


async def on_guild_role_update(before, after):
    if not enabled() or not _guild_ok(after) or after.managed:
        return  # управляемые роли меняет интеграция — не наше дело
    if _ROLE_SIG(before) == _ROLE_SIG(after):
        return

    guild = after.guild
    actor = await find_actor(guild, discord.AuditLogAction.role_update, after.id)
    state = await judge(guild, 'roleupdate', actor)

    if state in ('protected', 'exempt'):
        db.upsert_backup_role(serialize_role(after))
        return
    if state == 'punished':  # при unknown не откатываем — вдруг менял вайтлистнутый
        try:
            await after.edit(
                name=before.name,
                colour=before.colour,
                permissions=before.permissions,
                hoist=before.hoist,
                mentionable=before.mentionable,
                reason='[ANTINUKE] откат изменений роли',
            )
            await send_log(BOT, f'⟨ ↩️ ⟩ Откатил изменения роли <@&{after.id}>.')
        except discord.HTTPException as e:
            log.warning('[ANTINUKE] откат роли не удался: %s', e)


async def on_guild_role_delete(role):
    if not enabled() or not _guild_ok(role):
        return
    if role.managed:
        # интеграционная роль бота: Discord удаляет её сам при кике/бане бота —
        # это не нюк, не наказываем и не восстанавливаем
        db.del_backup_role(role.id)
        db.db.commit()
        return

    guild = role.guild
    row = db.get_backup_role(role.id)
    actor = await find_actor(guild, discord.AuditLogAction.role_delete, role.id)
    state = await judge(guild, 'roledelete', actor)

    if state in ('protected', 'exempt'):
        # легитимное удаление (вайтлист/владелец/бот) — убираем из бэкапа
        if row:
            db.del_backup_role(role.id)
            db.db.commit()
        return
    if state == 'unknown':
        # не смогли определить виновного — ничего не трогаем,
        # чтобы не восстанавливать то, что удалил вайтлистнутый
        return

    if row is None:
        # снапшота не было — собираем данные из самой удалённой роли (кэш события)
        try:
            row = serialize_role(role)
        except Exception:
            pass
    if row is None:
        await send_log(BOT, f"⟨ ⚠️ ⟩ Не смог восстановить роль **{getattr(role, 'name', '?')}**: нет данных.")
        return

    if await restore_role(guild, row):
        await send_log(BOT, f'⟨ ♻️ ⟩ Восстановил роль **{row["name"]}**.')
    else:
        await send_log(BOT, f'⟨ ⚠️ ⟩ НЕ смог восстановить роль **{row["name"]}** — '
                            f'проверь права бота (Manage Roles).')


async def on_member_update_roles(before: discord.Member, after: discord.Member):
    if not enabled() or not _guild_ok(after):
        return
    before_ids = {r.id for r in before.roles}
    after_ids = {r.id for r in after.roles}
    added = after_ids - before_ids
    removed = before_ids - after_ids
    if not added and not removed:
        return

    guild = after.guild
    actor = await find_actor(guild, discord.AuditLogAction.member_role_update, after.id)

    if added:
        await judge(guild, 'roleadd', actor)
    if removed:
        await judge(guild, 'roleremove', actor)


async def on_guild_update(before: discord.Guild, after: discord.Guild):
    if not enabled() or not _guild_ok(after):
        return
    # (категория, что откатывать при наказании)
    aspects = []
    if before.name != after.name:
        aspects.append(('serverchange', {'name': before.name}))
    if before.vanity_url_code != after.vanity_url_code:
        aspects.append(('linkdelete', {}))
    if before.icon != after.icon:
        aspects.append(('serverchange', None))

    seen = set()
    plan = []  # (category, revert_kwargs)
    for cat, revert in aspects:
        if cat in seen:
            if revert:
                for c, r in plan:
                    if c == cat and r is not None:
                        r.update(revert)
            continue
        seen.add(cat)
        plan.append((cat, dict(revert) if revert else None))

    for cat, revert in plan:
        actor = await find_actor(after, discord.AuditLogAction.guild_update)
        state = await judge(after, cat, actor)
        if state in ('protected', 'exempt'):
            revert = None  # легитимное изменение — не откатываем
        if revert:
            try:
                await after.edit(reason='[ANTINUKE] откат изменений сервера', **revert)
                await send_log(BOT, '⟨ ↩️ ⟩ Откатил изменения сервера.')
            except discord.HTTPException:
                pass


async def on_member_ban(guild: discord.Guild, user):
    if not enabled() or not _guild_ok(guild):
        return
    actor = await find_actor(guild, discord.AuditLogAction.ban, user.id)
    await judge(guild, 'ban', actor)


async def on_member_remove(member: discord.Member):
    """Кик отличаем от обычного выхода по свежей записи аудита."""
    if not enabled() or not _guild_ok(member):
        return
    guild = member.guild
    actor = await find_actor(guild, discord.AuditLogAction.kick, member.id)
    if actor is None:
        return  # просто вышел сам
    await judge(guild, 'kick', actor)


async def on_member_join_botadd(member: discord.Member):
    if not member.bot or not enabled() or not _guild_ok(member):
        return
    guild = member.guild
    inviter = await find_actor(guild, discord.AuditLogAction.bot_add, member.id)
    if inviter is None:
        return
    if BOT is None or inviter.id in (guild.owner_id, BOT.user.id):
        return
    oid = CONFIG.get('ownerId')
    if oid and str(inviter.id) == str(oid):
        return
    if db.wl_protected(inviter.id, 'botadd'):
        # даже вайтлистнутый не должен добавлять ботов:
        # бот банится всегда, а при повторном добавлении банят и приглашавшего
        cnt = db.bump_counter(guild.id, inviter.id, 'botadd')
        lim = get_limit('botadd')
        try:
            await guild.ban(member, reason='[ANTINUKE] добавлен ботом (botadd)', delete_message_seconds=0)
            await send_log(
                BOT,
                f'⟨ 🤖 ⟩ Бот <@{member.id}> забанен — его добавил <@{inviter.id}> (`botadd {cnt}/{lim}`).',
            )
        except discord.HTTPException:
            pass

        if cnt < lim:
            await send_log(
                BOT,
                f'⟨ ⚠️ ⟩ <@{inviter.id}> в вайтлисте, но добавил бота! '
                f'Предупреждение `{cnt}/{lim}` — при следующем разе будет бан.',
            )
            return

        db.reset_counter(guild.id, inviter.id, 'botadd')
        try:
            await guild.ban(inviter, reason=f'[ANTINUKE] повторное добавление бота (botadd {cnt}/{lim})',
                            delete_message_seconds=0)
            await send_log(
                BOT,
                f'⟨ 🔨 ⟩ **АНТИКРАШ**: <@{inviter.id}> (`{inviter.id}`) был в вайтлисте, '
                f'но повторно добавил бота и забанен (`botadd {cnt}/{lim}`).',
            )
        except discord.HTTPException as e_:
            log.warning('[ANTINUKE] не смог забанить вайтлистнутого %s: %s', inviter, e_)
        return

    # бот-нарушитель сразу банится, приглашающий — тоже
    try:
        await guild.ban(member, reason='[ANTINUKE] добавлен без вайтлиста (botadd)', delete_message_seconds=0)
    except discord.HTTPException:
        pass
    try:
        await guild.ban(inviter, reason='[ANTINUKE] добавил бота без вайтлиста (botadd)', delete_message_seconds=0)
        await send_log(
            BOT,
            f'⟨ 🤖 ⟩ Бот <@{member.id}> забанен, приглашавший <@{inviter.id}> забанен (botadd).',
        )
    except discord.HTTPException:
        pass


# ------------------------------ снапшот ------------------------------

async def snapshot_guild(guild: discord.Guild):
    chans = {str(c.id): serialize_channel(c) for c in guild.channels}
    roles = {str(r.id): serialize_role(r) for r in guild.roles if r != guild.default_role and not r.managed}
    db.sync_backup_channels(str(guild.id), chans)
    db.sync_backup_roles(str(guild.id), roles)


async def _snapshot_loop():
    interval = max(1, int(_cfg().get('snapshotIntervalMinutes', 5))) * 60
    while True:
        try:
            if BOT:
                for g in BOT.guilds:
                    if not is_allowed_guild(g.id):
                        continue
                    await snapshot_guild(g)
        except Exception:
            log.exception('[ANTINUKE][snapshot]')
        await asyncio.sleep(interval)


# ------------------------------ регистрация ------------------------------

async def register(bot):
    global BOT
    BOT = bot

    bot.add_listener(on_guild_channel_create, 'on_guild_channel_create')
    bot.add_listener(on_guild_channel_update, 'on_guild_channel_update')
    bot.add_listener(on_guild_channel_delete, 'on_guild_channel_delete')
    bot.add_listener(on_guild_role_create, 'on_guild_role_create')
    bot.add_listener(on_guild_role_update, 'on_guild_role_update')
    bot.add_listener(on_guild_role_delete, 'on_guild_role_delete')
    bot.add_listener(on_member_update_roles, 'on_member_update')
    bot.add_listener(on_webhooks_update, 'on_webhooks_update')
    bot.add_listener(on_guild_update, 'on_guild_update')
    bot.add_listener(on_member_join_botadd, 'on_member_join')
    bot.add_listener(on_member_ban, 'on_member_ban')
    bot.add_listener(on_member_remove, 'on_member_remove')

    bot.loop.create_task(_snapshot_loop())
