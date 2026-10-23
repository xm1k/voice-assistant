"""Regenerate README diagrams: uv run --no-project --with pillow render_diagrams.py."""

from pathlib import Path
import os

from PIL import Image, ImageDraw, ImageFont


OUTPUT = Path(__file__).resolve().parent
FONT_PATH = os.environ.get("DIAGRAM_FONT", "/System/Library/Fonts/Supplemental/Arial.ttf")
FONT = ImageFont.truetype(FONT_PATH, 22)
SMALL = ImageFont.truetype(FONT_PATH, 19)
TITLE = ImageFont.truetype(FONT_PATH, 32)


def render(name, title, subtitle, steps, notes):
    frames = []
    # Snake layout preserves a single readable path, including on the static image.
    positions = [(40, 145), (440, 145), (840, 145),
                 (840, 340), (440, 340), (40, 340)]
    for active in [None, *range(6)]:
        canvas = Image.new("RGB", (1240, 640), "#f1f5f9")
        draw = ImageDraw.Draw(canvas)
        draw.text((40, 30), title, font=TITLE, fill="#0f172a")
        draw.text((40, 82), subtitle, font=SMALL, fill="#475569")
        for i in range(5):
            x, y = positions[i]
            nx, ny = positions[i + 1]
            if ny != y:
                start, end = (x + 180, y + 135), (nx + 180, ny - 8)
                arrow = [end, (end[0] - 6, end[1] - 10), (end[0] + 6, end[1] - 10)]
            else:
                direction = 1 if nx > x else -1
                start = (x + (360 if direction == 1 else 0), y + 67)
                end = (nx - 8 if direction == 1 else nx + 368, ny + 67)
                arrow = [end, (end[0] - direction * 10, end[1] - 6),
                         (end[0] - direction * 10, end[1] + 6)]
            draw.line([start, end], fill="#64748b", width=3)
            draw.polygon(arrow, fill="#64748b")
        for i, ((x, y), (heading, lines)) in enumerate(zip(positions, steps)):
            highlighted = i == active
            draw.rounded_rectangle((x, y, x + 360, y + 135), radius=16,
                                   fill="#dbeafe" if highlighted else "#ffffff",
                                   outline="#2563eb" if highlighted else "#cbd5e1", width=3)
            draw.text((x + 18, y + 16), f"{i + 1}. {heading}", font=FONT, fill="#0f172a")
            for j, line in enumerate(lines):
                assert draw.textlength(line, font=SMALL) <= 324, line
                draw.text((x + 18, y + 54 + j * 26), line, font=SMALL, fill="#334155")
        draw.rounded_rectangle((40, 515, 1200, 607), radius=14, fill="#0f172a")
        caption = notes[active] if active is not None else "Общая схема • шаги обработки подсвечиваются по порядку"
        draw.text((62, 532), caption, font=SMALL, fill="#ffffff")
        draw.text((62, 568), "Упрощённый поток • подробности и исключения описаны в README v2", font=SMALL, fill="#cbd5e1")
        if active is None:
            canvas.save(OUTPUT / f"{name}.png")
        frames.append(canvas)
    frames[0].save(OUTPUT / f"{name}.gif", save_all=True, append_images=frames[1:],
                   duration=[2400] + [1900] * 6, loop=0, optimize=True, disposal=2)


def render_service_route():
    """Sequence view: arrows identify actual senders and receivers."""
    labels = ["Браузер", "gateway", "auth", "core", "PostgreSQL",
              "RabbitMQ", "assistant-worker", "LLM", "RAG"]
    steps = [
        (0, 1, "HTTPS: сообщение"),
        (1, 3, "HTTP: передача запроса"),
        (3, 2, "Проверка сессии → результат в core"),
        (3, 4, "Сохранить сообщение + job + outbox"),
        (3, 5, "Publisher: job_id в очередь chat"),
        (5, 6, "Доставка задания"),
        (6, 3, "Claim → контекст и claim_token"),
        (6, 7, "Контекст + схема → решение модели"),
        (6, 8, "Если нужен query: запрос данных"),
        (8, 3, "Фильтры → записи; для search также векторы из PostgreSQL"),
        (8, 6, "Результаты поиска"),
        (6, 7, "Данные → следующее решение модели"),
        (6, 3, "Validate → проверка с откатом"),
        (6, 5, "Решение в очередь llm_response"),
        (5, 3, "Results: доставка решения в core"),
        (3, 4, "Повторная проверка + атомарное сохранение"),
        (3, 1, "WebSocket: событие результата"),
        (1, 0, "Ответ и карточки в интерфейсе"),
    ]
    xs = [100 + i * 170 for i in range(len(labels))]
    frames = []
    for active in [None, *range(len(steps))]:
        canvas = Image.new("RGB", (1560, 1170), "#f1f5f9")
        draw = ImageDraw.Draw(canvas)
        draw.text((35, 25), "Маршрут текстового запроса по сервисам", font=TITLE, fill="#0f172a")
        draw.text((35, 75), "Сверху вниз — порядок обработки. Подсветка показывает текущий обмен.", font=SMALL, fill="#475569")
        for x, label in zip(xs, labels):
            draw.rounded_rectangle((x - 82, 115, x + 82, 166), radius=10, fill="#0f172a")
            draw.text((x, 140), label, anchor="mm", font=SMALL, fill="white")
            draw.line((x, 170, x, 1060), fill="#cbd5e1", width=2)
        for index, (source, target, label) in enumerate(steps):
            y = 205 + index * 48
            chosen = active == index
            if chosen:
                draw.rounded_rectangle((20, y - 29, 1540, y + 13), radius=8, fill="#dbeafe")
            color = "#2563eb" if chosen else "#475569"
            x1, x2 = xs[source], xs[target]
            draw.line((x1, y, x2, y), fill=color, width=4 if chosen else 2)
            direction = 1 if x2 > x1 else -1
            draw.polygon([(x2, y), (x2 - direction * 10, y - 6),
                          (x2 - direction * 10, y + 6)], fill=color)
            text = f"{index + 1:02d}  {label}"
            text_x = min(min(x1, x2) + 8, 1525 - draw.textlength(text, font=SMALL))
            draw.text((text_x, y - 25), text, font=SMALL, fill="#0f172a")
        draw.text((35, 1080), "Шаги 9–12 нужны для запроса данных и могут повторяться. Ответы HTTP указаны после →.", font=SMALL, fill="#334155")
        draw.text((35, 1115), "Publisher и results — процессы core. LLM предлагает решение; core владеет изменением записей.", font=SMALL, fill="#334155")
        if active is None:
            canvas.save(OUTPUT / "service-route.png")
        frames.append(canvas)
    frames[0].save(OUTPUT / "service-route.gif", save_all=True, append_images=frames[1:],
                   duration=[4000] + [4500] * len(steps), loop=0, optimize=True, disposal=2)


def render_service_blocks():
    """Fixed service map with a moving packet and explicit request/response steps."""
    nodes = {
        "browser": (55, 150, "Браузер", "Сообщение и результат"),
        "gateway": (505, 150, "gateway", "Вход в приложение"),
        "auth": (955, 150, "auth", "Проверка сессии"),
        "rabbit": (55, 370, "RabbitMQ", "Доставка заданий и решений"),
        "core": (505, 370, "core", "Данные и бизнес-правила"),
        "db": (955, 370, "PostgreSQL", "Записи, история, задания"),
        "worker": (55, 590, "assistant-worker", "Управление обработкой"),
        "rag": (505, 590, "RAG", "Получение данных"),
        "llm": (955, 590, "LLM", "Понимание запроса"),
    }
    steps = [
        ("browser", "gateway", "Сообщение пользователя", "«Перенеси завтрашнюю встречу с Анной на 17:00, на час»"),
        ("gateway", "core", "Передать сообщение", "Текст запроса и cookie сессии пользователя."),
        ("core", "auth", "Проверить сессию", "Кому принадлежит сессия? Какой у пользователя часовой пояс?"),
        ("auth", "core", "Вернуть пользователя", "Подтверждённый user_id и настройки пользователя."),
        ("core", "db", "Зарегистрировать запрос", "Одна транзакция: сообщение, job и outbox для отправки задания."),
        ("core", "rabbit", "Поставить задание в очередь", "Publisher core отправляет job_id в очередь chat."),
        ("rabbit", "worker", "Доставить задание", "Идентификатор задания для обработки сообщения."),
        ("worker", "core", "Забрать задание: claim", "Запросить право обработки и контекст диалога."),
        ("core", "worker", "Вернуть контекст", "claim_token, user_id, история, уточнение, время и timezone."),
        ("worker", "llm", "Определить действие", "Сообщение, контекст диалога и схема допустимых решений."),
        ("llm", "worker", "Сначала найти встречу", "Решение query: встречи за завтра; нужную запись ещё надо получить."),
        ("worker", "rag", "Выполнить запрос данных", "Тип записи и период; user_id и timezone берутся из задания."),
        ("rag", "core", "Получить записи по фильтрам", "Точная выборка встреч пользователя за указанный день."),
        ("core", "db", "Прочитать записи", "Данные пользователя для выборки; core учитывает даты и повторения."),
        ("db", "core", "Вернуть сохранённые данные", "Записи и версии для формирования результата запроса."),
        ("core", "rag", "Вернуть результат выборки", "Найденные встречи: id, version, время и остальные поля."),
        ("rag", "worker", "Передать найденные встречи", "Данные для выбора встречи с Анной и подготовки изменения."),
        ("worker", "llm", "Подготовить изменение", "Исходная просьба и найденные записи с актуальными версиями."),
        ("llm", "worker", "Предложить команду update", "id, version, новое начало 17:00 и окончание 18:00."),
        ("worker", "core", "Проверить решение: validate", "Команда и claim_token; предварительная проверка без сохранения."),
        ("core", "worker", "Вернуть результат проверки", "В успешном сценарии: valid=true. Изменение ещё не сохранено."),
        ("worker", "rabbit", "Опубликовать решение", "Очередь llm_response: job_id, claim_token и проверенная команда."),
        ("rabbit", "core", "Доставить решение", "Процесс results получает результат для окончательного применения."),
        ("core", "db", "Проверить и сохранить", "Повторная проверка, изменение встречи, ответ и события — атомарно."),
        ("core", "gateway", "Отправить событие результата", "WebSocket: обновление истории и карточка изменённой встречи."),
        ("gateway", "browser", "Показать результат", "Подтверждение из сохранённых данных: встреча перенесена на 17:00–18:00."),
    ]

    def route(source, target):
        x, y, *_ = nodes[source]
        tx, ty, *_ = nodes[target]
        if y == ty and abs(tx - x) == 450:
            return [(x + (340 if tx > x else 0), y + 55),
                    (tx + (0 if tx > x else 340), ty + 55)]
        if y == ty:
            return [(x + 170, y + 110), (x + 170, y + 150),
                    (tx + 170, ty + 150), (tx + 170, ty + 110)]
        start = (x + 170, y + (110 if ty > y else 0))
        end = (tx + 170, ty + (0 if ty > y else 110))
        mid_y = (start[1] + end[1]) / 2
        return [start, (start[0], mid_y), (end[0], mid_y), end]

    frames, durations = [], []
    for index, (source, target, heading, detail) in enumerate(steps):
        points = route(source, target)
        lengths = [abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(points, points[1:])]
        for phase in range(7):
            canvas = Image.new("RGB", (1350, 960), "#f1f5f9")
            draw = ImageDraw.Draw(canvas)
            draw.text((40, 25), "Запрос движется между сервисами", font=TITLE, fill="#0f172a")
            draw.text((40, 80), "Пример: перенос встречи • синий — отправитель • зелёный — получатель", font=SMALL, fill="#475569")
            draw.line(points, fill="#2563eb", width=5)
            end, before = points[-1], points[-2]
            dx, dy = end[0] - before[0], end[1] - before[1]
            if dx:
                direction = 1 if dx > 0 else -1
                arrow = [end, (end[0] - direction * 12, end[1] - 7), (end[0] - direction * 12, end[1] + 7)]
            else:
                direction = 1 if dy > 0 else -1
                arrow = [end, (end[0] - 7, end[1] - direction * 12), (end[0] + 7, end[1] - direction * 12)]
            draw.polygon(arrow, fill="#2563eb")
            for key, (x, y, label, purpose) in nodes.items():
                color = "#2563eb" if key == source else "#059669" if key == target else "#cbd5e1"
                fill = "#dbeafe" if key == source else "#d1fae5" if key == target else "white"
                draw.rounded_rectangle((x, y, x + 340, y + 110), radius=16, fill=fill, outline=color, width=3)
                draw.text((x + 20, y + 22), label, font=FONT, fill="#0f172a")
                draw.text((x + 20, y + 64), purpose, font=SMALL, fill="#475569")
            remaining = sum(lengths) * phase / 6
            for a, b, length in zip(points, points[1:], lengths):
                if remaining <= length and length:
                    ratio = remaining / length
                    px, py = a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio
                    draw.ellipse((px - 9, py - 9, px + 9, py + 9), fill="#2563eb", outline="white", width=2)
                    break
                remaining -= length
            draw.rounded_rectangle((35, 775, 1315, 920), radius=16, fill="#0f172a")
            caption = f"{index + 1:02d} / {len(steps)}   {nodes[source][2]} → {nodes[target][2]}: {heading}"
            assert draw.textlength(caption, font=FONT) < 1235
            assert draw.textlength(detail, font=SMALL) < 1235
            draw.text((57, 795), caption, font=FONT, fill="white")
            draw.text((57, 839), detail, font=SMALL, fill="#e2e8f0")
            draw.text((57, 882), "Успешный сценарий • ошибки и повторные обращения опущены • каждый шаг ≈ 5 секунд", font=SMALL, fill="#94a3b8")
            frames.append(canvas)
            durations.append(3800 if phase == 6 else 200)
            if index == 11 and phase == 3:
                canvas.save(OUTPUT / "service-blocks.png")
    frames[0].save(OUTPUT / "service-blocks.gif", save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True, disposal=1)


if __name__ == "__main__":
    render_service_blocks()
    render_service_route()
    render("request-flow", "Как запрос становится результатом",
           "Пример: «Завтра в 15:00 встреча с Анной на час»", [
               ("Сообщение", ["Текст или голос", "Аудио → распознанный текст"]),
               ("Core + очередь", ["Сообщение + job + outbox", "RabbitMQ доставляет задание"]),
               ("Контекст модели", ["История, уточнение, timezone", "Время исходного сообщения"]),
               ("Решение LLM", ["Уточнить / получить данные", "Ответить / предложить команды"]),
               ("Проверка и запись", ["Core проверяет дважды", "Пакет сохраняется целиком"]),
               ("Ответ пользователю", ["История и карточки результата", "Обновление через WebSocket"]),
           ], [
               "Голос после распознавания проходит тот же путь, что и текст.",
               "HTTP 202 подтверждает приём задания; изменение данных ещё не выполнено.",
               "Worker получает до 30 релевантных сообщений и контекст текущего запроса.",
               "Поиск возвращает данные модели; уточнение продолжает диалог с пользователем.",
               "Предварительная проверка откатывается; финальное применение — отдельная транзакция.",
               "Подтверждение изменения строится из сохранённых значений, а не обещания модели.",
           ])
    render("data-search", "Как помощник получает ваши данные",
           "Точные вопросы — по фильтрам. Поиск — по словам и смыслу.", [
               ("Вопрос → query", ["Модель задаёт тип запроса", "Период, тип записи, статус"]),
               ("RAG → core", ["Владелец взят из задания", "Core применяет фильтры"]),
               ("Выборка", ["Список / число / свободное время", "Для search — набор кандидатов"]),
               ("Рейтинг для search", ["Слова + векторы E5", "Для точных запросов пропуск"]),
               ("Результаты модели", ["Найденные записи и версии", "Данные для следующего шага"]),
               ("Ответ и ссылки", ["Проверка ссылок на записи", "Поиск сам данные не изменяет"]),
           ], [
               "Модель запрашивает данные через заданный контракт; SQL она не выполняет.",
               "user_id и timezone передаёт worker из контекста задания.",
               "Core учитывает период и повторения до ранжирования результатов поиска.",
               "Используются актуальные векторы; при сбое семантики остаётся поиск по словам.",
               "Модель получает результаты запроса, а не всю базу целиком.",
               "Изменение найденных данных требует отдельной команды и проверки в core.",
           ])
