"""Human confirmations and typed snapshots, built only from domain results."""

from backend.chat.domain import canonical
from backend.chat.responses import RecordCard

ACTIONS = {"create": "created", "update": "updated", "delete": "deleted"}
NOUNS = {"task": "Задача", "meeting": "Встреча", "reminder": "Напоминание"}
VERBS = {
    "created": {"task": "добавлена", "meeting": "добавлена", "reminder": "добавлено"},
    "updated": {"task": "обновлена", "meeting": "обновлена", "reminder": "обновлено"},
    "deleted": {"task": "удалена", "meeting": "удалена", "reminder": "удалено"},
}


def record_card(action, record):
    return RecordCard(action=action, record=record).model_dump(mode="json")


def visible_cards(cards):
    """An applied action supersedes its lookup card within the same message.

    A series action supersedes every lookup of that series; an occurrence action
    supersedes only that occurrence. Never merge records by their titles.
    """

    def key(card):
        record = card["record"]
        occurrence = record.get("occurrence_at")
        return record["id"], canonical(occurrence) if occurrence else None

    changed = {key(card) for card in cards if card["action"] != "found"}
    return [
        card
        for card in cards
        if card["action"] != "found"
        or (key(card) not in changed and (card["record"]["id"], None) not in changed)
    ]


def confirmation(cards):
    lines = []
    for card in cards:
        action, record = card["action"], card["record"]
        if action == "found":
            continue
        noun, verb = NOUNS[record["kind"]], VERBS[action][record["kind"]]
        scope = " (выбранное повторение)" if record.get("occurrence_at") else ""
        lines.append(f"{noun} {verb}{scope}: «{record['title']}».")
    return "\n".join(lines)
