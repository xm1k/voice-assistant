"""Safe, structured validation diagnostics shared by API and model feedback."""

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

RULES = {
    "Empty title": "Укажите название записи.",
    "Meeting requires start_at < end_at": "Окончание встречи должно быть позже начала; укажите обе даты.",
    "Reminder requires remind_at": "Укажите дату и время напоминания.",
    "Task uses due_date": "Для задачи укажите срок, а не время начала и окончания.",
    "Datetime requires timezone": "Укажите часовой пояс в дате и времени.",
    "Invalid timezone": "Укажите существующий часовой пояс.",
    "Repeating record requires a date": "Для повторения нужна дата начала.",
    "weekdays must be 0..6 (Monday..Sunday)": "Дни недели должны быть числами от 0 до 6.",
    "Both period boundaries required": "Укажите начало и конец периода.",
    "Period must be 0..366 days": "Конец периода должен быть не раньше начала, диапазон — не больше 366 дней.",
    "work_end must exceed work_start": "Конец рабочего интервала должен быть позже начала.",
    "Mutation requires exact id and version from query": "Перед изменением запросите идентификатор и версию записи.",
    "Occurrence requires occurrence_at": "Укажите дату выбранного повторения.",
    "query requires query and no commands": "В ветке query нужен запрос без команд и ссылок на записи.",
    "clarify requires question and no side effects": "В ветке clarify нужен вопрос без команд и запроса данных.",
    "ready requires answer or commands": "В ветке ready нужен ответ или команды, без вопроса и запроса данных.",
    "scope=occurrence is not valid for create; omit scope or use series": "Создавайте запись с областью series.",
    "Occurrence requires repeating record and exact date": "Изменение повторения требует серию и точную дату.",
    "Occurrence does not belong to series": "Указанное повторение не принадлежит серии.",
}


def validation_detail(exc, prefix=()):
    if isinstance(exc, (ValidationError, RequestValidationError)):
        issues = []
        for error in exc.errors():
            code = error["type"]
            original = error.get("msg", "").removeprefix("Value error, ")
            message = RULES.get(original)
            if not message:
                message = {
                    "missing": "Обязательное поле не заполнено.",
                    "extra_forbidden": "Неизвестное поле: используйте поля из схемы.",
                    "string_too_short": "Значение слишком короткое или пустое.",
                    "string_too_long": "Значение слишком длинное.",
                    "string_type": "Ожидается текстовое значение.",
                    "literal_error": "Выберите допустимое значение.",
                    "greater_than_equal": "Значение меньше допустимого.",
                    "less_than_equal": "Значение больше допустимого.",
                }.get(code, "Некорректное значение поля.")
            issues.append({"loc": [*prefix, *error.get("loc", ())], "code": code, "message": message})
        return {"message": "Проверьте параметры записи.", "errors": issues}
    if isinstance(exc, HTTPException):
        if isinstance(exc.detail, dict) and "errors" in exc.detail:
            detail = exc.detail
            return {**detail, "errors": [
                {**issue, "loc": [*prefix, *issue.get("loc", [])]} for issue in detail["errors"]
            ]}
        message = RULES.get(str(exc.detail))
        if not message:
            message = {
                404: "Запись или повторение больше недоступны. Запросите актуальные данные.",
                409: "Запись изменилась. Запросите актуальную версию.",
            }.get(exc.status_code, "Проверьте параметры записи.")
        return {"message": message, "errors": [
            {"loc": list(prefix), "code": f"record_{exc.status_code}", "message": message}
        ]}
    return {"message": "Проверьте параметры записи.", "errors": [
        {"loc": list(prefix), "code": "invalid_value", "message": "Некорректные параметры записи."}
    ]}


def public_validation_message(exc):
    detail = validation_detail(exc)
    labels = {"title": "Название", "start_at": "Начало", "end_at": "Окончание",
              "timezone": "Часовой пояс", "remind_at": "Время напоминания", "recurrence": "Повторение"}
    messages = []
    for issue in detail["errors"]:
        label = next((labels[key] for key in reversed(issue.get("loc", [])) if key in labels), None)
        messages.append(f"{label}: {issue['message']}" if label else issue["message"])
    return " ".join(dict.fromkeys(messages))[:2000]


def install_validation_handler(app):
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(status_code=422, content={"detail": validation_detail(exc)})
