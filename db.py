import os
import sqlite3
import time

from config import CONFIG


def _is_owner(user_id) -> bool:
    oid = CONFIG.get('ownerId')
    return bool(oid) and str(user_id) == str(oid)

_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(_DB_DIR, exist_ok=True)

db = sqlite3.connect(os.path.join(_DB_DIR, 'antipuzobot.db'))
db.row_factory = sqlite3.Row
db.execute('PRAGMA journal_mode = WAL')

db.executescript("""
CREATE TABLE IF NOT EXISTS bans (
  user_id    TEXT    NOT NULL,
  type       TEXT    NOT NULL CHECK (type IN ('rape', 'aban')),
  reason     TEXT,
  days       INTEGER,
  banned_by  TEXT,
  banned_at  INTEGER NOT NULL,
  expires_at INTEGER,
  active     INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, type)
);

CREATE TABLE IF NOT EXISTS dope (
  user_id   TEXT PRIMARY KEY,
  added_by  TEXT,
  added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS whitelist (
  user_id   TEXT PRIMARY KEY,
  added_by  TEXT,
  added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_roles (
  user_id   TEXT PRIMARY KEY,
  role_id   TEXT NOT NULL,
  saved_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS limits (
  category TEXT PRIMARY KEY,
  amount   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS levels (
  user_id   TEXT PRIMARY KEY,
  level     TEXT NOT NULL CHECK (level IN ('low', 'rape', 'semi', 'ceo')),
  added_by  TEXT,
  added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklist (
  user_id   TEXT PRIMARY KEY,
  added_by  TEXT,
  added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
  guild_id  TEXT NOT NULL,
  user_id   TEXT NOT NULL,
  category  TEXT NOT NULL,
  count     INTEGER NOT NULL DEFAULT 0,
  last_at   INTEGER NOT NULL,
  PRIMARY KEY (guild_id, user_id, category)
);

CREATE TABLE IF NOT EXISTS backup_channels (
  channel_id TEXT PRIMARY KEY,
  guild_id   TEXT NOT NULL,
  kind       TEXT NOT NULL,
  name       TEXT,
  type       INTEGER,
  position   INTEGER,
  parent_id  TEXT,
  topic      TEXT,
  nsfw       INTEGER DEFAULT 0,
  bitrate    INTEGER,
  user_limit INTEGER,
  rate_limit INTEGER,
  overwrites TEXT
);

CREATE TABLE IF NOT EXISTS backup_roles (
  role_id     TEXT PRIMARY KEY,
  guild_id    TEXT NOT NULL,
  name        TEXT,
  color       INTEGER,
  permissions INTEGER,
  hoist       INTEGER DEFAULT 0,
  mentionable INTEGER DEFAULT 0,
  position    INTEGER
);
""")

# миграция: колонка categories у вайтлиста
_cols = {r['name'] for r in db.execute('PRAGMA table_info(whitelist)').fetchall()}
if 'categories' not in _cols:
    db.execute('ALTER TABLE whitelist ADD COLUMN categories TEXT')
db.commit()


# ----------------------------- bans -----------------------------

def get_ban(user_id: int, btype: str):
    return db.execute(
        'SELECT * FROM bans WHERE user_id = ? AND type = ?', (str(user_id), btype)
    ).fetchone()


def get_active_ban(user_id: int, btype: str):
    row = get_ban(user_id, btype)
    return row if row and row['active'] == 1 else None


def set_ban(user_id, btype, reason=None, days=None, banned_by=None, expires_at=None):
    db.execute(
        """
        INSERT INTO bans (user_id, type, reason, days, banned_by, banned_at, expires_at, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(user_id, type) DO UPDATE SET
          reason = excluded.reason,
          days = excluded.days,
          banned_by = excluded.banned_by,
          banned_at = excluded.banned_at,
          expires_at = excluded.expires_at,
          active = 1
        """,
        (str(user_id), btype, reason, days, str(banned_by), int(time.time() * 1000), expires_at),
    )
    db.commit()


def deactivate_ban(user_id, btype):
    db.execute('UPDATE bans SET active = 0 WHERE user_id = ? AND type = ?', (str(user_id), btype))
    db.commit()


def remove_ban(user_id, btype) -> bool:
    cur = db.execute('DELETE FROM bans WHERE user_id = ? AND type = ?', (str(user_id), btype))
    db.commit()
    return cur.rowcount > 0


def expired_abans(now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    return db.execute(
        "SELECT * FROM bans WHERE type = 'aban' AND active = 1 "
        "AND expires_at IS NOT NULL AND expires_at <= ?",
        (now_ms,),
    ).fetchall()


def active_bans():
    return db.execute('SELECT * FROM bans WHERE active = 1').fetchall()


# ----------------------------- dope -----------------------------

def get_dope(user_id):
    return db.execute('SELECT * FROM dope WHERE user_id = ?', (str(user_id),)).fetchone()


def add_dope(user_id, by_id):
    db.execute(
        'INSERT OR IGNORE INTO dope (user_id, added_by, added_at) VALUES (?, ?, ?)',
        (str(user_id), str(by_id), int(time.time() * 1000)),
    )
    db.commit()


def del_dope(user_id):
    db.execute('DELETE FROM dope WHERE user_id = ?', (str(user_id),))
    db.commit()


# --------------------------- whitelist ---------------------------

def _now_ms():
    return int(time.time() * 1000)


def get_wl(user_id):
    return db.execute('SELECT * FROM whitelist WHERE user_id = ?', (str(user_id),)).fetchone()


def set_wl(user_id, by_id, categories=None):
    """categories: None = все категории, иначе строка 'cat1,cat2'."""
    db.execute(
        """
        INSERT INTO whitelist (user_id, added_by, added_at, categories) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          added_by = excluded.added_by,
          added_at = excluded.added_at,
          categories = excluded.categories
        """,
        (str(user_id), str(by_id), _now_ms(), categories),
    )
    db.commit()


def del_wl(user_id) -> bool:
    cur = db.execute('DELETE FROM whitelist WHERE user_id = ?', (str(user_id),))
    db.commit()
    return cur.rowcount > 0


def wl_entries():
    return db.execute('SELECT * FROM whitelist ORDER BY added_at').fetchall()


def wl_categories(user_id):
    """None если юзера нет; '*' если все категории; иначе set категорий."""
    row = get_wl(user_id)
    if not row:
        return None
    if not row['categories']:
        return '*'
    return {c for c in row['categories'].split(',') if c}


def wl_protected(user_id, category=None):
    """category=None — защита от .rape/.aban (любой вл). С категорией — защита конкретной категории антикраша."""
    if _is_owner(user_id):
        return True  # у владельца бота всегда полный вайтлист, снятию не подлежит
    cats = wl_categories(user_id)
    if cats is None:
        return False
    if category is None or cats == '*':
        return True
    return category in cats


# --------------------------- blacklist ---------------------------

def get_blacklisted(user_id):
    return db.execute('SELECT * FROM blacklist WHERE user_id = ?', (str(user_id),)).fetchone()


def add_blacklist(user_id, by_id):
    db.execute(
        'INSERT OR IGNORE INTO blacklist (user_id, added_by, added_at) VALUES (?, ?, ?)',
        (str(user_id), str(by_id), _now_ms()),
    )
    db.commit()


def del_blacklist(user_id) -> bool:
    cur = db.execute('DELETE FROM blacklist WHERE user_id = ?', (str(user_id),))
    db.commit()
    return cur.rowcount > 0


# -------------------------- saved roles --------------------------

def get_saved_role(user_id):
    return db.execute('SELECT * FROM saved_roles WHERE user_id = ?', (str(user_id),)).fetchone()


def set_saved_role(user_id, role_id):
    db.execute(
        """
        INSERT INTO saved_roles (user_id, role_id, saved_at) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET role_id = excluded.role_id, saved_at = excluded.saved_at
        """,
        (str(user_id), str(role_id), _now_ms()),
    )
    db.commit()


def del_saved_role(user_id):
    db.execute('DELETE FROM saved_roles WHERE user_id = ?', (str(user_id),))
    db.commit()


# ---------------------------- levels ----------------------------

def get_level(user_id):
    return db.execute('SELECT * FROM levels WHERE user_id = ?', (str(user_id),)).fetchone()


def set_level(user_id, level, by_id):
    db.execute(
        """
        INSERT INTO levels (user_id, level, added_by, added_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          level = excluded.level, added_by = excluded.added_by, added_at = excluded.added_at
        """,
        (str(user_id), level, str(by_id), _now_ms()),
    )
    db.commit()


def del_level(user_id) -> bool:
    cur = db.execute('DELETE FROM levels WHERE user_id = ?', (str(user_id),))
    db.commit()
    return cur.rowcount > 0


def all_levels():
    return db.execute('SELECT * FROM levels ORDER BY added_at').fetchall()


# ---------------------- counters (антикраш) ----------------------

def bump_counter(guild_id, user_id, category: str) -> int:
    """Счётчик действий per user+category, хранится в БД.
    По времени НЕ сбрасывается никогда — только reset_counter после нарушения."""
    row = db.execute(
        'SELECT count FROM counters WHERE guild_id = ? AND user_id = ? AND category = ?',
        (str(guild_id), str(user_id), category),
    ).fetchone()
    cnt = (row['count'] if row else 0) + 1
    db.execute(
        """
        INSERT INTO counters (guild_id, user_id, category, count, last_at) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id, category) DO UPDATE SET
          count = excluded.count, last_at = excluded.last_at
        """,
        (str(guild_id), str(user_id), category, cnt, int(time.time() * 1000)),
    )
    db.commit()
    return cnt


def reset_counter(guild_id, user_id, category: str):
    db.execute(
        'DELETE FROM counters WHERE guild_id = ? AND user_id = ? AND category = ?',
        (str(guild_id), str(user_id), category),
    )
    db.commit()


# ---------------------------- limits ----------------------------

def get_limit(category: str):
    row = db.execute('SELECT amount FROM limits WHERE category = ?', (category,)).fetchone()
    return row['amount'] if row else None


def set_limit(category: str, amount: int):
    db.execute(
        """
        INSERT INTO limits (category, amount) VALUES (?, ?)
        ON CONFLICT(category) DO UPDATE SET amount = excluded.amount
        """,
        (category, amount),
    )
    db.commit()


def del_limit(category: str):
    db.execute('DELETE FROM limits WHERE category = ?', (category,))
    db.commit()


def all_limits():
    return {r['category']: r['amount'] for r in db.execute('SELECT * FROM limits').fetchall()}


# ------------------------ backup channels ------------------------

def upsert_backup_channel(data: dict):
    db.execute(
        """
        INSERT INTO backup_channels (
          channel_id, guild_id, kind, name, type, position, parent_id,
          topic, nsfw, bitrate, user_limit, rate_limit, overwrites
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
          guild_id = excluded.guild_id, kind = excluded.kind, name = excluded.name,
          type = excluded.type, position = excluded.position, parent_id = excluded.parent_id,
          topic = excluded.topic, nsfw = excluded.nsfw, bitrate = excluded.bitrate,
          user_limit = excluded.user_limit, rate_limit = excluded.rate_limit,
          overwrites = excluded.overwrites
        """,
        (
            data['channel_id'], data['guild_id'], data['kind'], data['name'], data['type'],
            data['position'], data['parent_id'], data['topic'], data['nsfw'],
            data['bitrate'], data['user_limit'], data['rate_limit'], data['overwrites'],
        ),
    )
    db.commit()


def get_backup_channel(channel_id):
    return db.execute(
        'SELECT * FROM backup_channels WHERE channel_id = ?', (str(channel_id),)
    ).fetchone()


def del_backup_channel(channel_id):
    db.execute('DELETE FROM backup_channels WHERE channel_id = ?', (str(channel_id),))


def rekey_backup_channel(old_id, new_id, new_parent=None):
    if str(old_id) == str(new_id):
        return
    # событие channel_create могло уже записать новый канал — убираем конфликт
    db.execute('DELETE FROM backup_channels WHERE channel_id = ?', (str(new_id),))
    db.execute(
        'UPDATE backup_channels SET channel_id = ?, parent_id = COALESCE(?, parent_id) '
        'WHERE channel_id = ?',
        (str(new_id), new_parent, str(old_id)),
    )


def set_backup_parent(channel_id, parent_id):
    db.execute(
        'UPDATE backup_channels SET parent_id = ? WHERE channel_id = ?',
        (str(parent_id) if parent_id else None, str(channel_id)),
    )


def backup_children(parent_id):
    return db.execute(
        'SELECT * FROM backup_channels WHERE parent_id = ? ORDER BY position',
        (str(parent_id),),
    ).fetchall()


def sync_backup_channels(guild_id, items: dict):
    """items: {channel_id: data} — полная синхронизация бэкапа каналов сервера."""
    keep = list(items.keys())
    db.execute('DELETE FROM backup_channels WHERE guild_id = ?', (str(guild_id),))
    for data in items.values():
        db.execute(
            """
            INSERT INTO backup_channels (
              channel_id, guild_id, kind, name, type, position, parent_id,
              topic, nsfw, bitrate, user_limit, rate_limit, overwrites
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data['channel_id'], data['guild_id'], data['kind'], data['name'], data['type'],
                data['position'], data['parent_id'], data['topic'], data['nsfw'],
                data['bitrate'], data['user_limit'], data['rate_limit'], data['overwrites'],
            ),
        )
    db.commit()


# ------------------------- backup roles -------------------------

def upsert_backup_role(data: dict):
    db.execute(
        """
        INSERT INTO backup_roles (role_id, guild_id, name, color, permissions, hoist, mentionable, position)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(role_id) DO UPDATE SET
          guild_id = excluded.guild_id, name = excluded.name, color = excluded.color,
          permissions = excluded.permissions, hoist = excluded.hoist,
          mentionable = excluded.mentionable, position = excluded.position
        """,
        (
            data['role_id'], data['guild_id'], data['name'], data['color'],
            data['permissions'], data['hoist'], data['mentionable'], data['position'],
        ),
    )
    db.commit()


def get_backup_role(role_id):
    return db.execute('SELECT * FROM backup_roles WHERE role_id = ?', (str(role_id),)).fetchone()


def del_backup_role(role_id):
    db.execute('DELETE FROM backup_roles WHERE role_id = ?', (str(role_id),))


def rekey_backup_role(old_id, new_id):
    if str(old_id) == str(new_id):
        return
    # событие role_create могло уже записать новую роль — убираем конфликт
    db.execute('DELETE FROM backup_roles WHERE role_id = ?', (str(new_id),))
    db.execute(
        'UPDATE backup_roles SET role_id = ? WHERE role_id = ?', (str(new_id), str(old_id))
    )
    db.commit()


def sync_backup_roles(guild_id, items: dict):
    db.execute('DELETE FROM backup_roles WHERE guild_id = ?', (str(guild_id),))
    for data in items.values():
        db.execute(
            """
            INSERT INTO backup_roles (role_id, guild_id, name, color, permissions, hoist, mentionable, position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data['role_id'], data['guild_id'], data['name'], data['color'],
                data['permissions'], data['hoist'], data['mentionable'], data['position'],
            ),
        )
    db.commit()
