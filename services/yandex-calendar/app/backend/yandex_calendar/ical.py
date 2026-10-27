"""A complete record series occupies one RFC 5545 resource."""

import re
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from itertools import islice, zip_longest
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from dateutil.rrule import DAILY, MONTHLY, WEEKLY, rrule, rrulestr
from icalendar import Alarm, Calendar, Event, Timezone


def recurrence_rule(data, origin):
    """Same series semantics as core's domain.recurrence_rule; keep them in step."""
    recurrence = data["recurrence"]
    options = {"dtstart": origin, "interval": recurrence.get("interval", 1)}
    if recurrence.get("count"):
        options["count"] = recurrence["count"]
    if recurrence.get("until"):
        options["until"] = datetime.combine(date.fromisoformat(recurrence["until"]), time.max, origin.tzinfo)
    if recurrence.get("weekdays"):
        options["byweekday"] = recurrence["weekdays"]
    return rrule({"daily": DAILY, "weekly": WEEKLY, "monthly": MONTHLY}[recurrence["frequency"]], **options)


@lru_cache(maxsize=128)
def timezone_component(zone_name, first_date, last_date):
    zone = ZoneInfo(zone_name)
    if last_date.year < 9999:
        return Timezone.from_tzinfo(
            zone, tzid=zone_name, first_date=first_date, last_date=last_date
        ).to_ical()
    # icalendar's transition search probes year10000 and raises OverflowError.
    # TZdb's final POSIX future rules repeat on the Gregorian400-year cycle.
    # Generate9999 using the library's equivalent9599 calendar, then shift only
    # the library-generated DTSTART/RDATE values; no custom timezone rules.
    component = Timezone.from_tzinfo(
        zone, tzid=zone_name, first_date=min(first_date, date(9998, 1, 1)), last_date=date(9998, 12, 31)
    )
    tail = Timezone.from_tzinfo(zone, tzid=zone_name, first_date=date(9599, 1, 1), last_date=date(9600, 1, 1))
    for raw in tail.subcomponents:
        shifted = deepcopy(raw)
        start = shifted.decoded("dtstart")
        del shifted["DTSTART"]
        shifted.add("dtstart", start.replace(year=start.year + 400))
        if "RDATE" in shifted:
            values = [item.dt.replace(year=item.dt.year + 400) for item in shifted["RDATE"].dts]
            del shifted["RDATE"]
            shifted.add("rdate", values)
        component.add_component(shifted)
    component["COMMENT"] = f"Timezone transitions cover {first_date} through {last_date}."
    return component.to_ical()


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def anchor(data):
    return "start_at" if data.get("start_at") else "remind_at" if data.get("remind_at") else "due_date"


def local(value, zone):
    return date.fromisoformat(value) if len(value) == 10 else instant(value).astimezone(zone)


def require_minute(value):
    if value and len(value) != 10:
        parsed = instant(value)
        if parsed.second or parsed.microsecond:
            raise ValueError("calendar_precision_unsupported")


def event(data, uid, operation_id):
    zone = ZoneInfo(data.get("timezone", "Europe/Moscow"))
    value = data.get(anchor(data))
    if not value:
        raise ValueError("record_has_no_date")
    require_minute(value)
    require_minute(data.get("end_at"))
    start = local(value, zone)
    component = Event()
    component.add("uid", uid)
    # Fixed metadata makes retries byte-stable; SEQUENCE follows the core version elsewhere.
    component.add("dtstamp", datetime(2000, 1, 1, tzinfo=timezone.utc))
    component.add("summary", data["title"])
    component.add("description", data.get("notes") or "")
    component.add("dtstart", start)
    end = (
        local(data["end_at"], zone)
        if data.get("end_at")
        else start + (timedelta(minutes=30) if isinstance(start, datetime) else timedelta(days=1))
    )
    component.add("dtend", end)
    component.add("status", "CONFIRMED")
    component.add("class", "PRIVATE")
    if data.get("reminder_minutes") is not None:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", data["title"])
        alarm.add("trigger", -timedelta(minutes=data["reminder_minutes"]))
        component.add_component(alarm)
    return component


def serialize(data, exceptions, uid, operation_id):
    calendar = Calendar()
    calendar.add("prodid", "-//Golos//Calendar//RU")
    calendar.add("version", "2.0")
    zone = ZoneInfo(data.get("timezone", "Europe/Moscow"))
    master = event(data, uid, operation_id)
    calendar.add_component(master)
    start = master.decoded("dtstart")
    origin = start if isinstance(start, datetime) else datetime.combine(start, time.min, zone)
    recurrence = data.get("recurrence")
    dates = [origin.date()]
    if recurrence:
        options = {"freq": recurrence["frequency"].upper(), "interval": recurrence.get("interval", 1)}
        if recurrence.get("count") and recurrence.get("until"):
            bounded = {
                **data,
                "recurrence": {key: value for key, value in recurrence.items() if key != "count"},
            }
            count = sum(1 for _ in islice(recurrence_rule(bounded, origin), recurrence["count"]))
            if not count:
                raise ValueError("empty_recurrence")
            options["count"] = count
        elif recurrence.get("count"):
            options["count"] = recurrence["count"]
        elif recurrence.get("until"):
            finish = datetime.combine(date.fromisoformat(recurrence["until"]), time(23, 59, 59), zone)
            options["until"] = (
                finish.astimezone(timezone.utc) if isinstance(start, datetime) else finish.date()
            )
        if recurrence.get("weekdays"):
            options["byday"] = [
                ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][value] for value in recurrence["weekdays"]
            ]
        master.add("rrule", options)
        rule_data = (
            {**data, "recurrence": {key: value for key, value in recurrence.items() if key != "until"}}
            if recurrence.get("count") and recurrence.get("until")
            else data
        )
        if rule_data is not data:
            rule_data["recurrence"]["count"] = options["count"]
        rule = recurrence_rule(rule_data, origin)
        if recurrence.get("count"):
            last = next(reversed(list(islice(rule, options.get("count", recurrence["count"])))), None)
            if last:
                dates.append(last.date())
        if recurrence.get("until"):
            dates.append(date.fromisoformat(recurrence["until"]))
        # Unlike RRULE expansion, DTSTART is always part of an RFC recurrence set.
        if rule.before(origin, inc=True) != origin:
            master.add("exdate", start)
        for exception in sorted(exceptions, key=lambda item: item["occurrence_at"]):
            stamp, override = exception["occurrence_at"], exception["data"]
            require_minute(stamp)
            original = local(stamp, zone)
            original_instant = (
                original if isinstance(original, datetime) else datetime.combine(original, time.min, zone)
            )
            dates.append(original_instant.date())
            if override.get("deleted") or override.get("status", "active") != "active":
                master.add("exdate", original)
                continue
            if rule.before(original_instant, inc=True) != original_instant:
                master.add("rdate", original)
            item = {**data, anchor(data): stamp}
            if data.get("end_at"):
                duration = instant(data["end_at"]) - instant(data["start_at"])
                item["end_at"] = (original_instant + duration).isoformat()
            item.update(override)
            child = event(item, uid, operation_id)
            child.add("recurrence-id", original)
            dates.append(
                child.decoded("dtstart").date()
                if isinstance(child.decoded("dtstart"), datetime)
                else child.decoded("dtstart")
            )
            calendar.add_component(child)
    if isinstance(start, datetime):
        # Cover the full stored recurrence/exception horizon with library-generated transitions.
        # Yandex misreads future-only definitions despite valid event coverage.
        # Keep an installed-library historical baseline; retain earlier records.
        lower = min(min(dates).replace(month=1, day=1), date(1970, 1, 1))
        upper_year = (
            9999
            if recurrence and not recurrence.get("count") and not recurrence.get("until")
            else max(dates).year + 1
        )
        upper = date(min(upper_year, 9999), 12, 31)
        zones = {zone.key}
        for item in calendar.walk("VEVENT"):
            for property_name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                value = item.get(property_name)
                if value is not None and getattr(value.dt, "tzinfo", None) is not None:
                    name = getattr(value.dt.tzinfo, "key", None)
                    if name:
                        zones.add(name)
        for name in sorted(zones):
            calendar.add_component(Timezone.from_ical(timezone_component(name, lower, upper)))
    return calendar.to_ical().decode()


IGNORED = {
    "PRODID",
    "DTSTAMP",
    "LAST-MODIFIED",
    "CREATED",
    "SEQUENCE",
}


def canonical(content):
    def value(item):
        if hasattr(item, "dt"):
            item = item.dt
        if isinstance(item, datetime):
            if item.tzinfo is None:
                return ("local_datetime", item.isoformat())
            return ("datetime", item.astimezone(timezone.utc).isoformat(), getattr(item.tzinfo, "key", ""))
        if isinstance(item, date):
            return ("date", item.isoformat())
        if isinstance(item, timedelta):
            return ("duration", item.total_seconds())
        if hasattr(item, "dts"):
            return tuple(sorted((value(part) for part in item.dts), key=repr))
        if isinstance(item, dict):
            return tuple(
                sorted(
                    (str(key).upper(), value(part))
                    for key, part in item.items()
                    if not (str(key).upper() == "INTERVAL" and list(part) == [1])
                )
            )
        if isinstance(item, (list, tuple)):
            return tuple(sorted((value(part) for part in item), key=repr))
        return str(item)

    def component(item):
        properties = []
        date_groups = {}
        defaults = (
            {"CALSCALE": "GREGORIAN", "METHOD": "PUBLISH"}
            if item.name == "VCALENDAR"
            else {
                "STATUS": "CONFIRMED",
                "TRANSP": "OPAQUE",
                "CLASS": "PUBLIC",
                "PRIORITY": "0",
                "DESCRIPTION": "",
            }
            if item.name == "VEVENT"
            else {}
        )

        def instant_values(part):
            if isinstance(part, tuple) and part and part[0] == "datetime":
                return part[:2]
            if isinstance(part, tuple):
                return tuple(instant_values(entry) for entry in part)
            return part

        for key, raw in item.items():
            if key.upper() in IGNORED:
                continue
            if key.upper() in defaults and str(raw).upper() == defaults[key.upper()]:
                continue
            entries = raw if isinstance(raw, list) else [raw]
            for entry in entries:
                params = tuple(
                    sorted(
                        (str(name).upper(), value(part))
                        for name, part in getattr(entry, "params", {}).items()
                        if not (
                            str(name).upper() in ("TZID", "VALUE")
                            and (hasattr(entry, "dt") or hasattr(entry, "dts"))
                        )
                    )
                )
                parsed = value(entry)
                if key.upper() in ("EXDATE", "RDATE") and hasattr(entry, "dts"):
                    date_groups.setdefault((key.upper(), params), []).extend(instant_values(parsed))
                else:
                    properties.append(
                        (
                            key.upper(),
                            params,
                            instant_values(parsed) if key.upper() == "RECURRENCE-ID" else parsed,
                        )
                    )
        properties.extend(
            (key, params, tuple(sorted(set(values), key=repr)))
            for (key, params), values in date_groups.items()
        )
        return (
            item.name,
            tuple(sorted(properties, key=repr)),
            tuple(sorted((component(child) for child in item.subcomponents), key=repr)),
        )

    return component(Calendar.from_ical(content))


def equivalent(left, right):
    try:
        return canonical(left) == canonical(right)
    except (ValueError, TypeError, KeyError):
        return False


@lru_cache(maxsize=64)
def normalized_timezone(actual, intended, old):
    if old:
        return canonical(actual) == canonical(old)
    if canonical(actual) == canonical(intended):
        return True
    current = Timezone.from_ical(actual)
    expected = Timezone.from_ical(intended)
    name = str(current.get("TZID", ""))
    if name != str(expected.get("TZID", "")):
        return False
    reference = ZoneInfo(name)
    # Accept only the observed canonical provider definition, never fetch TZURL.
    if (
        set(current) != {"TZID", "TZURL", "X-LIC-LOCATION"}
        or str(current["TZURL"]) != "http://tzurl.org/zoneinfo/" + name
        or str(current["X-LIC-LOCATION"]) != name
    ):
        return False
    allowed = {"DTSTART", "RDATE", "RRULE", "TZNAME", "TZOFFSETFROM", "TZOFFSETTO"}
    if any(
        part.name not in ("STANDARD", "DAYLIGHT") or set(part) - allowed for part in current.subcomponents
    ):
        return False
    # The provider's recognized IANA contract covers recurrence between these
    # probes. Retain its complete definition+ETag to fence every later edit.
    boundaries = re.findall(r"\d{4}-\d{2}-\d{2}", str(expected.get("COMMENT", "")))
    years = {date.fromisoformat(value).year for value in boundaries}
    years.update(part.decoded("DTSTART").year for part in expected.subcomponents)
    parsed = current.to_tz(lookup_tzid=False)
    for year in years:
        for month in (1, 4, 7, 10):
            probe = datetime(year, month, 15, 12)
            if probe.replace(tzinfo=parsed).utcoffset() != probe.replace(tzinfo=reference).utcoffset():
                return False
    return True


def finite_rules_equivalent(actual, intended):
    def sequence(component):
        rule = component.get("RRULE")
        if rule is None or not (rule.get("COUNT") or rule.get("UNTIL")):
            return None
        start = component.decoded("DTSTART")
        if not isinstance(start, datetime):
            start = datetime.combine(start, time.min)
        return (
            value.astimezone(timezone.utc) if value.tzinfo else value
            for value in rrulestr(rule.to_ical().decode(), dtstart=start)
        )

    try:
        if getattr(actual.get("RRULE"), "params", {}) != getattr(intended.get("RRULE"), "params", {}):
            return False

        def pattern(component):
            return {
                name: tuple(sorted(str(value).upper() for value in values))
                for name, values in component.get("RRULE", {}).items()
                if name not in ("COUNT", "UNTIL") and not (name == "INTERVAL" and list(values) == [1])
            }

        if pattern(actual) != pattern(intended):
            return False
        left, right = sequence(actual), sequence(intended)
        if left is None or right is None:
            return False
        if actual["RRULE"] == intended["RRULE"] and actual.decoded("DTSTART") == intended.decoded("DTSTART"):
            return True
        missing = object()
        return all(first == second for first, second in zip_longest(left, right, fillvalue=missing))
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


def server_equivalent(actual, expected, baseline, username, calendar_name):
    """Only observed Yandex-generated additions may normalize a saved intent.

    Once a resource has a baseline, its unmanaged fields must remain unchanged.
    ETags independently fence every later preflight and conditional mutation.
    """

    def key(item):
        recurrence = item.get("RECURRENCE-ID")
        if recurrence is None:
            return "master"
        value = recurrence.dt
        return (
            value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else value.isoformat()
        )

    try:
        current = Calendar.from_ical(actual)
        desired = Calendar.from_ical(expected)
        old_calendar = Calendar.from_ical(baseline) if baseline else None
        old = {key(item): item for item in old_calendar.walk("VEVENT")} if old_calendar else {}
        current_zones = {str(item["TZID"]): item for item in current.walk("VTIMEZONE")}
        desired_zones = {str(item["TZID"]): item for item in desired.walk("VTIMEZONE")}
        old_zones = (
            {str(item["TZID"]): item for item in old_calendar.walk("VTIMEZONE")} if old_calendar else {}
        )
        if (
            len(current_zones) != len(current.walk("VTIMEZONE"))
            or current_zones.keys() != desired_zones.keys()
        ):
            return False
        for name, zone in current_zones.items():
            if not normalized_timezone(
                zone.to_ical(),
                desired_zones[name].to_ical(),
                old_zones[name].to_ical() if name in old_zones else None,
            ):
                return False
            current.subcomponents.remove(zone)
            desired.subcomponents.remove(desired_zones[name])
        desired_events = {key(item): item for item in desired.walk("VEVENT")}
        for item in current.walk("VEVENT"):
            identity = key(item)
            target = desired_events.get(identity)
            if target is None:
                return False
            if finite_rules_equivalent(item, target):
                target["RRULE"] = deepcopy(item["RRULE"])
            for alarm in target.subcomponents:
                if (
                    alarm.name != "VALARM"
                    or str(alarm.get("ACTION", "")).upper() != "DISPLAY"
                    or str(alarm.get("DESCRIPTION", "")) != str(target.get("SUMMARY", ""))
                ):
                    continue
                missing_description = any(
                    candidate.name == "VALARM"
                    and str(candidate.get("ACTION", "")).upper() == "DISPLAY"
                    and "DESCRIPTION" not in candidate
                    and candidate.get("TRIGGER") is not None
                    and alarm.get("TRIGGER") is not None
                    and candidate["TRIGGER"].to_ical() == alarm["TRIGGER"].to_ical()
                    for candidate in item.subcomponents
                )
                if missing_description:
                    alarm.pop("DESCRIPTION", None)
            previous = old.get(identity)
            for name in ("URL", "ORGANIZER", "CATEGORIES"):
                if previous is not None and name in previous:
                    target[name] = deepcopy(previous[name])
                    continue
                raw = item.get(name)
                if raw is None:
                    continue
                if set(getattr(raw, "params", {})) - ({"CN"} if name == "ORGANIZER" else set()):
                    return False
                if name == "URL":
                    url = urlsplit(str(raw))
                    query = parse_qs(url.query, keep_blank_values=True)
                    if (
                        url.scheme != "https"
                        or url.hostname != "calendar.yandex.ru"
                        or url.port not in (None, 443)
                        or url.username
                        or url.password
                        or url.fragment
                        or url.path != "/event"
                        or set(query) != {"event_id"}
                        or len(query["event_id"]) != 1
                        or not query["event_id"][0].isdigit()
                    ):
                        return False
                elif name == "ORGANIZER":
                    if str(raw).casefold() != ("mailto:" + username).casefold():
                        return False
                elif [str(category) for category in raw.cats] != [calendar_name]:
                    return False
                target[name] = deepcopy(raw)
        return equivalent(current.to_ical(), desired.to_ical())
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def uid(content):
    values = Calendar.from_ical(content).walk("VEVENT")
    return str(values[0]["UID"]) if values else ""
