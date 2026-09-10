import json
import os
import sys

_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')


def _load():
    if not os.path.exists(_CFG_PATH):
        print('[CONFIG] config.json не найден рядом с main.py')
        sys.exit(1)
    try:
        with open(_CFG_PATH, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception as e:
        print(f'[CONFIG] config.json битый: {e}')
        sys.exit(1)

    if not cfg.get('token') or 'ВСТАВЬ' in cfg.get('token', ''):
        print('[CONFIG] Впиши токен бота в config.json')
        sys.exit(1)
    return cfg


CONFIG = _load()


def is_allowed_guild(guild_id) -> bool:
    """True, если бот должен работать на этом сервере (guildId в конфиге)."""
    gid = CONFIG.get('guildId')
    if not gid or str(gid) == 'ID_СЕРВЕРА':
        return True
    return str(guild_id) == str(gid)
