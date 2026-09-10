import re

import discord

from config import CONFIG
import db

EMO_OK = '⟨ ✔ ⟩'
EMO_ERR = '⟨ 𝘅 ⟩'

# уровни доступа хранятся в БД бота (таблица levels)
LEVEL_VALUE = {'low': 1, 'rape': 2, 'semi': 3, 'ceo': 4}
DAY_MS = 86_400_000

# категории антикраша (вайтлист и лимиты)
CATEGORIES = [
    'rolecreate', 'roleupdate', 'roledelete', 'roleadd', 'roleremove',
    'channelcreate', 'channelupdate', 'channeldelete',
    'categorycreate', 'categoryupdate', 'categorydelete',
    'webhookcreate', 'webhookupdate', 'webhookdelete',
    'botadd', 'serverchange', 'linkdelete',
    'ban', 'kick',
]
CATEGORY_SET = set(CATEGORIES)

ID_RE = re.compile(r'^(?:<@!?(\d{15,25})>|(\d{15,25}))$')


def ok(description: str):
    return discord.Embed(color=0x57F287, description=f'{EMO_OK} {description}')


def err(description: str):
    return discord.Embed(color=0xED4245, description=f'{EMO_ERR} {description}')


def parse_id(arg) -> int | None:
    if not arg:
        return None
    m = ID_RE.match(str(arg))
    if not m:
        return None
    return int(m.group(1) or m.group(2))


async def fetch_target(guild: discord.Guild, arg):
    uid = parse_id(arg)
    if uid is None:
        return None
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except (discord.NotFound, discord.HTTPException):
            member = None
    return uid, member


def _is_owner(user_id) -> bool:
    oid = CONFIG.get('ownerId')
    return bool(oid) and str(oid) == str(user_id)


def _member_level(member) -> int:
    if _is_owner(member.id):
        return 4
    if member.id == member.guild.owner_id:
        return 4
    if CONFIG.get('adminBypass', True) and member.guild_permissions.administrator:
        return 4
    row = db.get_level(member.id)
    return LEVEL_VALUE.get(row['level'], 0) if row else 0


def has_access(member, cmd: str) -> bool:
    """Доступ по уровню из БД: у команды указан минимальный уровень,
    выше по иерархии тоже проходят (ceo > semi > rape > low)."""
    if member is None:
        return False
    lvl = _member_level(member)
    need = (CONFIG.get('access') or {}).get(cmd)
    if not need:
        return False
    try:
        required = min(LEVEL_VALUE[n] for n in need if n in LEVEL_VALUE)
    except ValueError:
        return False
    return lvl >= required


def is_protected(user_id: int) -> bool:
    return _is_owner(user_id) or bool(db.get_dope(user_id) or db.get_wl(user_id))


def guard_punish(msg: discord.Message, uid: int, member) -> discord.Embed | None:
    if uid == msg.author.id:
        return err('Нельзя выдать это самому себе.')
    if member and (member.bot or uid == msg.client.user.id):
        return err('На ботов наказание не выдаётся.')
    if is_protected(uid):
        return err('Пользователь защищён (.dope / whitelist).')
    return None


# ----------------------------- команды -----------------------------

async def cmd_rape(msg: discord.Message, args: list[str]):
    t = await fetch_target(msg.guild, args[0] if args else None)
    if not t:
        return err('Использование: `.rape <@юзер|id> [причина]`')
    uid, member = t
    guard = guard_punish(msg, uid, member)
    if guard:
        return guard

    reason = ' '.join(args[1:]) if len(args) > 1 else 'без причины'
    db.set_ban(uid, 'rape', reason=reason, banned_by=msg.author.id, expires_at=None)

    try:
        await msg.guild.ban(discord.Object(id=uid), reason=f'[RAPE] {msg.author}: {reason}')
    except discord.HTTPException as e:
        db.remove_ban(uid, 'rape')
        return err(f'Не удалось забанить: {e}')
    return ok(
        f'**РЕЙП** выдан <@{uid}> (`{uid}`) — **навсегда**.\n'
        f'Причина: {reason}\n'
        f'При разбане он будет забанен автоматически при заходе на сервер.'
    )


async def cmd_aban(msg: discord.Message, args: list[str]):
    t = await fetch_target(msg.guild, args[0] if args else None)
    if not t:
        return err('Использование: `.aban <@юзер|id> <дней> [причина]`')
    uid, member = t
    guard = guard_punish(msg, uid, member)
    if guard:
        return guard

    try:
        days = int(args[1])
    except (IndexError, ValueError):
        days = 0
    if days <= 0:
        return err('Использование: `.aban <@юзер|id> <дней> [причина]` — второй аргумент должен быть числом дней.')

    reason = ' '.join(args[2:]) if len(args) > 2 else 'без причины'
    expires_at = int(__import__('time').time() * 1000) + days * DAY_MS

    db.set_ban(uid, 'aban', reason=reason, days=days, banned_by=msg.author.id, expires_at=expires_at)

    try:
        await msg.guild.ban(discord.Object(id=uid), reason=f'[ABAN {days}д] {msg.author}: {reason}')
    except discord.HTTPException as e:
        db.remove_ban(uid, 'aban')
        return err(f'Не удалось забанить: {e}')
    return ok(
        f'**АБАН** выдан <@{uid}> (`{uid}`) на **{days} дн.** (до <t:{expires_at // 1000}:f>).\n'
        f'Причина: {reason}\n'
        f'Если его разбанят раньше срока — он будет забанен автоматически.'
    )


async def cmd_dope(msg: discord.Message, args: list[str]):
    t = await fetch_target(msg.guild, args[0] if args else None)
    if not t or not t[1]:
        return err('Юзер должен находиться на сервере. Использование: `.dope <@юзер|id>`')
    uid, member = t
    if db.get_blacklisted(uid):
        return err(f'<@{uid}> в **блэклисте** — выдача .dope запрещена (сними: `.unblacklist`).')
    if db.get_dope(uid):
        return err('У пользователя уже есть защита (.dope).')

    db.add_dope(uid, msg.author.id)

    highest = None
    candidates = [r for r in member.roles if r != msg.guild.default_role and not r.managed]
    if candidates:
        highest = max(candidates, key=lambda r: r.position)

    if highest:
        db.set_saved_role(uid, highest.id)
        saved_name = f'<@&{highest.id}>'
    else:
        db.del_saved_role(uid)
        saved_name = 'нет (только @everyone)'

    return ok(
        f'**DOPE** выдан <@{uid}> — неуязвимость от `.rape` и `.aban`.\n'
        f'Сохранена самая высокая роль на момент выдачи: {saved_name}. '
        f'Если её снимут — она вернётся автоматически.'
    )


async def cmd_wl(msg: discord.Message, args: list[str]):
    if not CONFIG.get('wlGive', True):
        return err('Выдача вайтлиста отключена (wl = off)')

    if args and args[0].lower() == 'list':
        entries = db.wl_entries()
        if not entries:
            return err('Вайтлист пуст.')
        lines = []
        for row in entries[:40]:
            if not row['categories']:
                cats = '**все категории**'
            else:
                cats = ', '.join(f'`{c}`' for c in row['categories'].split(','))
            lines.append(f'<@{row["user_id"]}> — {cats}')
        return discord.Embed(color=0x5865F2, title='Вайтлист', description='\n'.join(lines))

    t = await fetch_target(msg.guild, args[0] if args else None)
    if not t:
        return err(
            'Использование: `.wl <@юзер|id> [категории...]` — без категорий выдаются **все**.\n'
            'Категории: ' + ', '.join(f'`{c}`' for c in CATEGORIES)
        )
    uid, _member = t

    tokens = []
    for tok in args[1:]:
        tokens.extend(p for p in tok.split(',') if p)
    low = [x.lower() for x in tokens]

    # ---- снятие категорий через none ----
    if 'none' in low:
        if len(low) == 1:
            if not db.get_wl(uid):
                return err(f'<@{uid}> нет в вайтлисте.')
            db.del_wl(uid)
            return ok(f'<@{uid}> удалён из **всех** категорий вайтлиста.')

        bad = [x for x in low if x != 'none' and x not in CATEGORY_SET]
        if bad:
            return err('Неизвестные категории: ' + ', '.join(f'`{b}`' for b in bad))
        to_remove = {x for x in low if x != 'none'}

        row = db.get_wl(uid)
        if not row:
            return err(f'<@{uid}> нет в вайтлисте.')
        current = set() if not row['categories'] else set(row['categories'].split(','))
        current -= to_remove
        removed_str = ', '.join(f'`{c}`' for c in sorted(to_remove))

        if not current:
            db.del_wl(uid)
            return ok(f'Снял категории {removed_str}. Категорий не осталось — <@{uid}> убран из вайтлиста.')
        db.set_wl(uid, msg.author.id, ','.join(sorted(current)))
        return ok(
            f'Снял категории {removed_str} у <@{uid}>.\n'
            f'Осталось: ' + ', '.join(f'`{c}`' for c in sorted(current))
        )

    # ---- выдача/обновление (блэклист запрещает) ----
    if db.get_blacklisted(uid):
        return err(f'<@{uid}> в **блэклисте** — выдача вайтлиста запрещена (сними: `.unblacklist`).')

    if low:
        bad = [x for x in low if x not in CATEGORY_SET]
        if bad:
            return err(
                'Неизвестные категории: ' + ', '.join(f'`{b}`' for b in bad) + '\n'
                'Доступные: ' + ', '.join(f'`{c}`' for c in CATEGORIES) + ' (или `none` для снятия)'
            )

    cats = ','.join(dict.fromkeys(low)) or None
    db.set_wl(uid, msg.author.id, cats)
    shown = '**все категории**' if cats is None else ', '.join(f'`{c}`' for c in cats.split(','))
    return ok(f'<@{uid}> в **вайтлисте**. Защита: {shown}.')


async def cmd_limit(msg: discord.Message, args: list[str]):
    sub = args[0].lower() if args else ''

    if sub == 'list':
        overrides = db.all_limits()
        lines = []
        for c in CATEGORIES:
            v = overrides.get(c)
            lines.append(f'`{c}`: {v if v is not None else "1 (по умолчанию)"}')
        embed = discord.Embed(color=0x5865F2, title='Лимиты антикраша', description='\n'.join(lines))
        embed.set_footer(text='Счётчик не сбрасывается по времени — только после нарушения | .limit set <категория|all> <число>')
        return embed

    if sub == 'set':
        cat = args[1].lower() if len(args) > 1 else ''
        try:
            amount = int(args[2])
        except (IndexError, ValueError):
            amount = 0
        if amount < 1:
            return err('Использование: `.limit set <категория|all> <число>` — число ≥ 1.')
        if cat == 'all':
            for c in CATEGORIES:
                db.set_limit(c, amount)
            return ok(f'Лимит для **всех категорий** установлен: **{amount}**.')
        if cat not in CATEGORY_SET:
            return err('Категория: `' + '`, `'.join(CATEGORIES) + '` или `all`.')
        db.set_limit(cat, amount)
        return ok(f'Лимит `{cat}` установлен: **{amount}**.')

    if sub in ('remove', 'reset'):
        cat = args[1].lower() if len(args) > 1 else ''
        if cat == 'all':
            for c in CATEGORIES:
                db.del_limit(c)
            return ok('Все лимиты сброшены на значения по умолчанию (1).')
        if cat not in CATEGORY_SET:
            return err('Категория: `' + '`, `'.join(CATEGORIES) + '` или `all`.')
        db.del_limit(cat)
        return ok(f'Лимит `{cat}` сброшен на значение по умолчанию (1).')

    return err('Использование: `.limit set <категория|all> <число>` / `.limit remove <категория|all>` / `.limit list`')


def make_unban_cmd(btype: str, label: str, usage: str):
    async def handler(msg: discord.Message, args: list[str]):
        t = await fetch_target(msg.guild, args[0] if args else None)
        if not t:
            return err(f'Использование: `{usage}`')
        uid, _member = t

        existed = db.remove_ban(uid, btype)
        if not existed:
            return err(f'У <@{uid}> нет активного **{label}**.')

        unbanned = False
        try:
            await msg.guild.fetch_ban(discord.Object(id=uid))
            await msg.guild.unban(discord.Object(id=uid), reason=f'[UN{btype.upper()}] {msg.author}')
            unbanned = True
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass

        text = f'**{label}** снят с <@{uid}>.'
        text += ' Пользователь разбанен.' if unbanned else ' (на момент снятия не был в бане)'
        return ok(text)

    return handler


cmd_unrape = make_unban_cmd('rape', 'RAPE', '.unrape <@юзер|id>')
cmd_unaban = make_unban_cmd('aban', 'АБАН', '.unaban <@юзер|id>')


async def cmd_undope(msg: discord.Message, args: list[str]):
    t = await fetch_target(msg.guild, args[0] if args else None)
    if not t:
        return err('Использование: `.undope <@юзер|id>`')
    uid, _member = t
    if not db.get_dope(uid):
        return err(f'У <@{uid}> нет защиты (.dope).')
    db.del_dope(uid)
    db.del_saved_role(uid)
    return ok(f'Защита (.dope) снята с <@{uid}>.')


async def cmd_unwl(msg: discord.Message, args: list[str]):
    t = await fetch_target(msg.guild, args[0] if args else None)
    if not t:
        return err('Использование: `.unwl <@юзер|id>`')
    uid, _member = t
    if not db.get_wl(uid):
        return err(f'<@{uid}> нет в вайтлисте.')
    db.del_wl(uid)
    return ok(f'<@{uid}> убран из вайтлиста.')


async def cmd_blacklist(msg: discord.Message, args: list[str]):
    t = await fetch_target(msg.guild, args[0] if args else None)
    if not t:
        return err('Использование: `.blacklist <@юзер|id>` — запрет на получение .wl и .dope')
    uid, _member = t
    if db.get_blacklisted(uid):
        return err(f'<@{uid}> уже в блэклисте.')
    db.add_blacklist(uid, msg.author.id)
    return ok(f'<@{uid}> добавлен в **блэклист** — не сможет получить `.wl` и `.dope`.')


async def cmd_unblacklist(msg: discord.Message, args: list[str]):
    t = await fetch_target(msg.guild, args[0] if args else None)
    if not t:
        return err('Использование: `.unblacklist <@юзер|id>`')
    uid, _member = t
    if not db.get_blacklisted(uid):
        return err(f'<@{uid}> нет в блэклисте.')
    db.del_blacklist(uid)
    return ok(f'<@{uid}> убран из **блэклиста**.')


async def cmd_info(msg: discord.Message, args: list[str]):
    t = await fetch_target(msg.guild, args[0] if args else None)
    if not t:
        return err('Использование: `.info <@юзер|id>`')
    uid, _member = t

    rape = db.get_active_ban(uid, 'rape')
    aban = db.get_active_ban(uid, 'aban')
    dope = db.get_dope(uid)
    wl = db.get_wl(uid)
    bl = db.get_blacklisted(uid)
    saved = db.get_saved_role(uid)

    lines = [f'Статус <@{uid}> (`{uid}`):']
    lines.append(f"• RAPE: активен (причина: {rape['reason'] or '—'})" if rape else '• RAPE: нет')
    if aban:
        lines.append(
            f"• ABAN: активен до <t:{aban['expires_at'] // 1000}:f> ({aban['days']} дн., причина: {aban['reason'] or '—'})"
        )
    else:
        lines.append('• ABAN: нет')
    lines.append('• DOPE: есть (неуязвим)' if dope else '• DOPE: нет')
    if wl:
        cats = '**все категории**' if not wl['categories'] else ', '.join(f'`{c}`' for c in wl['categories'].split(','))
        lines.append(f'• WHITELIST: есть ({cats})')
    else:
        lines.append('• WHITELIST: нет')
    lines.append('• BLACKLIST: да (запрет на .wl/.dope)' if bl else '• BLACKLIST: нет')
    if saved:
        lines.append(f"• Сохранённая роль: <@&{saved['role_id']}>")

    return discord.Embed(color=0x5865F2, description='\n'.join(lines))


async def cmd_lvl(msg: discord.Message, args: list[str]):
    sub = args[0].lower() if args else ''

    if sub == 'list':
        rows = db.all_levels()
        if not rows:
            return err('Уровни никому не выданы.')
        lines = [f"<@{r['user_id']}> — `{r['level']}`" for r in rows[:50]]
        return discord.Embed(color=0x5865F2, title='Уровни доступа', description='\n'.join(lines))

    if sub == 'set':
        t = await fetch_target(msg.guild, args[1] if len(args) > 1 else None)
        if not t or not t[1]:
            return err('Юзер должен быть на сервере. Использование: `.lvl set <@юзер|id> <low|rape|semi|ceo>`')
        uid, member = t
        level = (args[2].lower() if len(args) > 2 else '')
        if level not in LEVEL_VALUE:
            return err('Уровень: `low`, `rape`, `semi`, `ceo`.')
        if uid == msg.guild.owner_id:
            return err('Владельцу сервера уровни не нужны.')
        if _is_owner(uid):
            return err('У этого юзера постоянный CEO доступ — его нельзя менять.')
        my = _member_level(msg.author)
        if LEVEL_VALUE[level] > my:
            return err('Нельзя выдать уровень выше своего.')
        db.set_level(uid, level, msg.author.id)
        return ok(f'Уровень **{level}** выдан <@{uid}>.')

    if sub == 'remove':
        t = await fetch_target(msg.guild, args[1] if len(args) > 1 else None)
        if not t:
            return err('Использование: `.lvl remove <@юзер|id>`')
        uid, _member = t
        if _is_owner(uid):
            return err('У этого юзера постоянный CEO доступ — его нельзя снять.')
        if not db.get_level(uid):
            return err(f'У <@{uid}> нет уровня доступа.')
        db.del_level(uid)
        return ok(f'Уровень доступа снят с <@{uid}>.')

    t = await fetch_target(msg.guild, args[0] if args else None)
    if t and t[0] is not None:
        row = db.get_level(t[0])
        return ok(f'Уровень <@{t[0]}>: ' + (f"**{row['level']}**" if row else 'не выдан'))

    return err(
        'Использование: `.lvl set <@юзер|id> <low|rape|semi|ceo>` / '
        '`.lvl remove <@юзер|id>` / `.lvl list` / `.lvl <@юзер|id>`'
    )


async def cmd_list(msg: discord.Message, args: list[str]):
    CAP = 25

    def fmt_mentions(ids, extra=None):
        if not ids:
            return '—'
        parts = []
        for i, uid in enumerate(ids[:CAP]):
            if extra:
                parts.append(extra[i])
            else:
                parts.append(f'<@{uid}>')
        tail = f' …и ещё {len(ids) - CAP}' if len(ids) > CAP else ''
        return ', '.join(parts) + tail

    lines = ['**УРОВНИ ДОСТУПА**']
    groups = {'ceo': [], 'semi': [], 'rape': [], 'low': []}
    for r in db.all_levels():
        if r['level'] in groups:
            groups[r['level']].append(r['user_id'])
    for lvl in ('ceo', 'semi', 'rape', 'low'):
        lines.append(f'**{lvl.upper()}**: ' + fmt_mentions(groups[lvl]))

    bans = db.active_bans()
    rapes = [b['user_id'] for b in bans if b['type'] == 'rape']
    abans = [b for b in bans if b['type'] == 'aban']

    lines.append('')
    lines.append('**НАКАЗАНИЯ**')
    lines.append('**RAPE (вечный)**: ' + fmt_mentions(rapes))
    lines.append(
        '**ABAN**: '
        + fmt_mentions(
            [b['user_id'] for b in abans],
            [f"<@{b['user_id']}> ({b['days']} дн., до <t:{b['expires_at'] // 1000}:f>)" for b in abans],
        )
    )

    return discord.Embed(color=0x5865F2, description='\n'.join(lines))


HELP_LINES = [
    (('rape',), '`.rape <@юзер|id> [причина]`'),
    (('aban',), '`.aban <@юзер|id> <дней> [причина]`'),
    (('dope',), '`.dope <@юзер|id>`'),
    (('wl',), '`.wl <@юзер|id> [категории...]`'),
    (('limit',), '`.limit set <категория|all> <число>` / `.limit remove <...>` / `.limit list`'),
    (('lvl',), '`.lvl set/remove/list`'),
    (('unrape', 'unaban', 'undope', 'unwl'), '`.unrape / .unaban / .undope / .unwl <@юзер|id>`'),
    (('blacklist',), '`.blacklist <@юзер|id>` / `.unblacklist <@юзер|id>`'),
    (('info',), '`.info <@юзер|id>`'),
    (('list',), '`.list`'),
]


async def cmd_help(msg: discord.Message, args: list[str]):
    available = []
    for names, line in HELP_LINES:
        group = names if isinstance(names, tuple) else (names,)
        if any(has_access(msg.author, c) for c in group):
            available.append(line)

    embed = discord.Embed(
        color=0x5865F2,
        title='Команды',
        description='\n'.join(available) if available else 'Нет доступных команд.',
    )
    embed.set_footer(text=f'Уровни доступа: ceo > semi > rape > low | префикс: {CONFIG["prefix"]}')
    return embed


HANDLERS = {
    'rape': cmd_rape,
    'aban': cmd_aban,
    'dope': cmd_dope,
    'wl': cmd_wl,
    'limit': cmd_limit,
    'lvl': cmd_lvl,
    'unrape': cmd_unrape,
    'unaban': cmd_unaban,
    'undope': cmd_undope,
    'unwl': cmd_unwl,
    'blacklist': cmd_blacklist,
    'unblacklist': cmd_unblacklist,
    'info': cmd_info,
    'list': cmd_list,
    'help': cmd_help,
}
