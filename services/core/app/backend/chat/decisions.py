"""One transactional implementation for decision preview and final application."""

import json

from backend.chat.domain import RecurrenceBoundRequired, change, meeting_conflict_pairs, resolve_record
from backend.chat.presentation import ACTIONS, record_card, visible_cards
from backend.common.errors import validation_detail
from fastapi import HTTPException
from pydantic import ValidationError


class MeetingConflict(Exception):
    """A proposed batch needs a user question before any effects can commit."""

    def __init__(self, commands, overlaps, *, requires_recurrence_bound=False):
        self.proposal = {"commands": commands, "conflicts": overlaps}
        if requires_recurrence_bound:
            self.proposal["requires_recurrence_bound"] = True
        super().__init__("Meeting conflict requires clarification")

def apply_decision(db, user_id, decision, timezone_name, *, consent=None):
    changes, cards, seen_refs, targets, created_ids = [], [], set(), {}, {}
    command_cards = []
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
            if command.operation == "create":
                created_ids[snapshot["id"]] = f"command:{index}"
            # Renaming or editing notes must not ask about an unchanged schedule.
            if command.operation != "delete" and (
                command.operation == "create"
                or set(fields) & {"kind", "start_at", "end_at", "recurrence", "status", "timezone"}
            ):
                targets[(snapshot["id"], command.occurrence_at if command.scope == "occurrence" else None)] = index
            if deleted:
                snapshot = {**deleted, "version": snapshot["version"], "conflicts": []}
            command_cards.append((ACTIONS[command.operation], snapshot))
        except (HTTPException, ValidationError, ValueError) as exc:
            raise HTTPException(422, validation_detail(exc, ("commands", index, "fields"))) from exc

    # Inspect the final batch, after every create/update/delete. Caller owns rollback.
    overlaps, checked = [], {}
    commands = [command.model_dump(mode="json", exclude_none=True) for command in decision.commands]

    def summary(item):
        return {**{key: item.get(key) for key in (
            "version", "title", "start_at", "end_at", "occurrence_at"
        )}, "id": created_ids.get(item["id"], item["id"])}

    for (record_id, occurrence_at), index in targets.items():
        checked[(record_id, occurrence_at)] = []
        try:
            proposed = resolve_record(db, user_id, record_id, occurrence_at=occurrence_at)
        except HTTPException as exc:
            if exc.status_code == 404:  # A later command deleted this target.
                continue
            raise
        try:
            pairs = meeting_conflict_pairs(db, user_id, proposed)
        except RecurrenceBoundRequired as exc:
            raise MeetingConflict(commands, [{"command_index": index, "proposed": summary(exc.proposed),
                                             "existing": summary(exc.existing)}],
                                  requires_recurrence_bound=True) from exc
        for occurrence, existing in pairs:
            checked[(record_id, occurrence_at)].append(existing)
            overlaps.append({"command_index": index, "proposed": summary(occurrence),
                             "existing": summary(existing)})
    overlaps.sort(key=lambda value: json.dumps(value, sort_keys=True))
    if overlaps and not (
        consent and decision.conflict_confirmation == consent.get("token")
        and commands == consent.get("commands") and overlaps == consent.get("conflicts")
    ):
        raise MeetingConflict(commands, overlaps)
    for command, snapshot in zip(decision.commands, changes):
        key = (snapshot["id"], command.occurrence_at if command.scope == "occurrence" else None)
        if command.operation != "delete" and key in checked:
            snapshot["conflicts"] = checked[key]
    cards.extend(record_card(action, snapshot) for action, snapshot in command_cards)
    return changes, visible_cards(cards)
