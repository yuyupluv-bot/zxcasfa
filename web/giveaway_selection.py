"""Selection rules for an admin-configured giveaway winner list."""
from __future__ import annotations

import re
import secrets

# Store VK numeric IDs, not names: names are not guaranteed to be unique.
VK_ID = re.compile(r"(?:https?://)?(?:www\.)?(?:vk\.com/)?(?:id)?([1-9][0-9]{0,18})/?", re.IGNORECASE)


def parse_winner_ids(raw: str) -> list[int]:
    ids = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        match = VK_ID.fullmatch(line)
        if not match or int(match.group(1)) > 9223372036854775807:
            raise ValueError(f"Неверный VK ID или ссылка: {line[:100]}")
        vk_id = int(match.group(1))
        if vk_id in ids:
            raise ValueError(f"VK ID {vk_id} указан больше одного раза")
        ids.append(vk_id)
        if len(ids) > 50:
            raise ValueError("Можно указать не более 50 победителей")
    return ids


def select_winners(candidates: list[dict], fixed_ids: list[int], count: int) -> list[dict]:
    """Configured participants take places in order; fill other places randomly.

    Invalid/ineligible configured participants block the draw, rather than
    silently selecting someone else or circumventing the participation rules.
    """
    if len(fixed_ids) > count:
        raise ValueError("Назначенных победителей больше, чем указано мест. Увеличьте количество мест или измените список.")
    by_vk = {person["vk_id"]: person for person in candidates}
    missing = [str(vk_id) for vk_id in fixed_ids if vk_id not in by_vk]
    if missing:
        raise ValueError("Назначенные участники не подходят под условия или заблокированы: " + ", ".join(missing))
    if len(candidates) < count:
        raise ValueError(f"Под условия подходят только {len(candidates)} человек, а победителей указано {count}")
    fixed = [by_vk[vk_id] for vk_id in fixed_ids]
    fixed_set = set(fixed_ids)
    remaining = [person for person in candidates if person["vk_id"] not in fixed_set]
    return fixed + secrets.SystemRandom().sample(remaining, count - len(fixed))
