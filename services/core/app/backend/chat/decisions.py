"""One transactional implementation for decision preview and final application."""

from backend.chat.domain import change, resolve_record
from backend.chat.presentation import ACTIONS, record_card, visible_cards
from backend.common.errors import validation_detail
from fastapi import HTTPException
from pydantic import ValidationError


def apply_decision(db, user_id, decision, timezone_name):
    changes, cards, seen_refs = [], [], set()
    for index, ref in enumerate(decision.record_refs):
        key = (ref.id, ref.occurrence_at)
        if key not in seen_refs:
            try:
                record = resolve_record(db, user_id, ref.id, version=ref.version, occurrence_at=ref.occurrence_at)
                cards.append(record_card("found", record))
            except (HTTPException, ValidationError, ValueError) as exc:
                raise HTTPException(422, validation_detail(exc, ("record_refs", index))) from exc
            seen_refs.add(key)
    for index, command in enumerate(decision.commands):
        try:
            deleted = (
                resolve_record(db, user_id, command.id, version=command.version,
                               occurrence_at=command.occurrence_at if command.scope == "occurrence" else None)
                if command.operation == "delete" else None
            )
            fields = command.fields.model_dump(mode="json", exclude_unset=True, exclude_none=True)
            fields.update({key: None for key in command.clear_fields})
            snapshot = change(db, user_id, command.operation, fields, command.id, command.version,
                              command.scope, command.occurrence_at, timezone_name)
            changes.append(snapshot)
            if deleted:
                snapshot = {**deleted, "version": snapshot["version"], "conflicts": []}
            cards.append(record_card(ACTIONS[command.operation], snapshot))
        except (HTTPException, ValidationError, ValueError) as exc:
            raise HTTPException(422, validation_detail(exc, ("commands", index, "fields"))) from exc
    return changes, visible_cards(cards)
