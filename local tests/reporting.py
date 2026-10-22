"""Self-contained HTML report for the public API scenario suite."""

import json
import re
import time
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

LABELS = {"passed": "Прошли", "failed": "Не прошли", "error": "Ошибки", "skipped": "Пропущены", "notrun": "Не завершены"}
ROW_LABELS = {"passed": "Пройден", "failed": "Не пройден", "error": "Ошибка", "skipped": "Пропущен", "notrun": "Не завершён"}


class HtmlReport:
    def __init__(self, path, base_url):
        self.path = Path(path)
        self.base_url = base_url
        self.started = datetime.now(ZoneInfo("Europe/Moscow"))
        self.clock = time.monotonic()
        self.cases = {}
        self.collection_errors = []
        self.written = False
        source = Path(__file__).with_name("README.md").read_text()
        self.titles = dict(re.findall(r"^\|\s*(\d+)\s*\|\s*(.*?)\s*\|$", source, re.MULTILINE))

    def collect(self, items):
        for item in items:
            name = item.name
            number = re.match(r"test_(\d+)_", name)
            title = self.titles.get(number[1], name) if number else name
            self.cases[item.nodeid] = {"name": name, "title": title, "phases": {}, "trace": []}

    def record(self, item, report, api):
        case = self.cases[item.nodeid]
        secrets = getattr(api, "_report_secrets", [])

        def redact(value):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "<REDACTED>")
            return value

        case["phases"][report.when] = {
            "outcome": report.outcome, "duration": report.duration,
            "detail": redact(str(report.longrepr)) if report.longrepr else "",
        }
        case["trace"] = json.loads(redact(json.dumps(getattr(api, "trace", []), ensure_ascii=False)))

    @staticmethod
    def status(case):
        phases = case["phases"]
        if any(p["outcome"] == "failed" for name, p in phases.items() if name != "call"):
            return "error"
        if phases.get("call", {}).get("outcome") == "failed":
            return "failed"
        if any(p["outcome"] == "skipped" for p in phases.values()):
            return "skipped"
        if phases.get("call", {}).get("outcome") == "passed" and phases.get("teardown", {}).get("outcome") == "passed":
            return "passed"
        return "notrun"

    def write(self, exitstatus):
        elapsed = time.monotonic() - self.clock
        counts = {key: 0 for key in LABELS}
        rows = []
        for nodeid, case in sorted(self.cases.items(), key=lambda pair: pair[1]["name"]):
            status = self.status(case)
            counts[status] += 1
            duration = sum(p["duration"] for p in case["phases"].values())
            diagnostics = "".join(
                f'<section><h3>{escape({"setup": "Подготовка", "call": "Сценарий", "teardown": "Очистка"}.get(name, name))}</h3>'
                f'<pre>{escape(phase["detail"])}</pre></section>'
                for name, phase in case["phases"].items() if phase["detail"]
            )
            turns = []
            for index, turn in enumerate(case["trace"], 1):
                result = turn.get("result") or {}
                if not isinstance(result, dict):
                    result = {}
                turns.append(
                    f'<section class="turn"><h3>Реплика {index}</h3>'
                    f'<p class="user">{escape(str(turn.get("text", "")))}</p>'
                    f'<p>{escape(str(result.get("answer") or turn.get("error") or "Ответ не получен"))}</p>'
                    f'<p class="meta">Задание {escape(str(turn.get("job_id", "—")))} / {escape(str(turn.get("status", "—")))}</p>'
                    f'<details><summary>Данные задания</summary><pre>{escape(json.dumps(turn, ensure_ascii=False, indent=2))}</pre></details></section>'
                )
            trace = "".join(turns) or '<p class="meta">Реплики не отправлялись.</p>'
            rows.append(
                f'<details class="case" data-status="{status}">'
                f'<summary><span class="badge {status}">{ROW_LABELS[status]}</span>'
                f'<span class="title">{escape(case["title"])}</span><span class="duration">{duration:.2f} с</span></summary>'
                f'<div class="body"><p class="meta">{escape(nodeid)}</p>{diagnostics}'
                f'<h3>Диалог с ассистентом</h3>{trace}</div></details>'
            )
        buttons = '<button type="button" data-filter="all" aria-pressed="true">Все <b>' + str(len(self.cases)) + '</b></button>'
        buttons += "".join(f'<button type="button" data-filter="{key}" aria-pressed="false">{label} <b>{counts[key]}</b></button>' for key, label in LABELS.items())
        segments = "".join(f'<span class="{key}" style="flex-grow:{count}"></span>' for key, count in counts.items() if count)
        collection = "".join(f'<pre>{escape(error)}</pre>' for error in self.collection_errors)
        if collection:
            collection = '<section><h2>Ошибки сборки тестов</h2>' + collection + '</section>'
        headline = "Все сценарии прошли" if self.cases and counts["passed"] == len(self.cases) and int(exitstatus) == 0 else "Запуск требует внимания"
        values = {"title": headline, "server": escape(self.base_url),
                  "date": self.started.strftime("%d.%m.%Y %H:%M:%S MSK"),
                  "duration": f"{elapsed:.1f}", "exit": str(int(exitstatus)),
                  "buttons": buttons, "segments": segments, "collection": collection, "rows": "".join(rows)}
        content = re.sub(r"\{\{(\w+)\}\}", lambda match: values[match[1]], TEMPLATE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content, encoding="utf-8")
        self.written = True


TEMPLATE = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Отчёт LLM-тестов</title><style>
:root{color-scheme:light;--ink:#14283f;--muted:#52657b;--line:#d9e2eb;--green:#08745f;--red:#b4233c}
*{box-sizing:border-box}body{margin:0;background:#eef3f7;color:var(--ink);font:16px/1.6 "Segoe UI",Arial,sans-serif}
main{max-width:1120px;margin:40px auto;padding:0 24px}header{padding:36px;background:#14283f;color:white;border-radius:16px 16px 0 0}
h1{font-size:clamp(26px,4vw,40px);line-height:1.2;margin:8px 0 20px;letter-spacing:-1px}header p{margin:6px 0;color:#d2e1f0;overflow-wrap:anywhere}
header .product{font-size:16px;color:#90e0c6;margin:0}.bar{display:flex;height:8px;background:#b8c5d2}.bar span{flex-basis:0}
.bar .passed{background:#34b897}.bar .failed{background:#d74761}.bar .error{background:#ce7a23}.bar .skipped{background:#8e9dae}.bar .notrun{background:#637a91}
.panel{padding:26px 32px 32px;background:white;border-radius:0 0 16px 16px}nav{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
button,input{font:inherit}button{padding:8px 13px;border:1px solid var(--line);background:white;color:var(--ink);border-radius:8px;cursor:pointer}
button[aria-pressed=true]{background:var(--ink);color:white;border-color:var(--ink)}button b{margin-left:6px}
:focus-visible{outline:3px solid #087ea4;outline-offset:3px}label{display:block;font-weight:600;margin-bottom:6px}
input{width:100%;padding:12px 14px;border:1px solid #95a8bb;border-radius:8px;color:var(--ink);background:white;margin-bottom:8px}
.meta,.duration{color:var(--muted);font-size:13px;overflow-wrap:anywhere}.case{border-top:1px solid var(--line)}
.case>summary{display:flex;align-items:center;gap:14px;padding:19px 0;cursor:pointer;list-style:none}.case>summary::-webkit-details-marker{display:none}
.case>summary::after{content:'+';font-size:22px;flex-shrink:0}.case[open]>summary::after{content:'−'}
.title{flex:1;font-weight:600;line-height:1.4}.duration{white-space:nowrap}.badge{font-size:12px;font-weight:700;padding:4px 9px;border-radius:5px;white-space:nowrap;min-width:100px;text-align:center}
.badge.passed{background:#dff3ec;color:#075b49}.badge.failed{background:#fce7ec;color:#9c1830}.badge.error{background:#fff0db;color:#805000}
.badge.skipped,.badge.notrun{background:#e8edf3;color:#465c73}.body{padding:0 0 25px 114px}.body>h3{margin-top:24px}
h3{font-size:15px;margin:14px 0 6px}.turn{border-left:3px solid #b8cbdc;padding:2px 18px 14px;margin-top:18px}.turn p{margin:7px 0;white-space:pre-wrap;overflow-wrap:anywhere}.user{font-weight:600}
pre{font:12px/1.65 Consolas,"Liberation Mono",monospace;background:#f0f4f8;border:1px solid var(--line);border-radius:8px;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:440px;overflow:auto}
summary{cursor:pointer}footer{color:var(--muted);font-size:13px;padding:18px 0}#empty{padding:24px 0}[hidden]{display:none!important}
@media(max-width:640px){main{margin:16px auto;padding:0 12px}header,.panel{padding:22px 18px}.case>summary{flex-wrap:wrap;gap:9px}.title{flex-basis:calc(100% - 120px)}.body{padding-left:0}.duration{margin-left:109px}}
@media print{body{background:white}main{margin:0;max-width:none}nav,.search{display:none}.panel,header{padding:16px}.case{break-inside:avoid}}
</style></head><body><main>
<header><p class="product">Голосовой ассистент / API и LLM</p><h1>{{title}}</h1><p>{{server}}</p>
<p>{{date}} · {{duration}} с · код завершения {{exit}}</p></header><div class="bar" aria-hidden="true">{{segments}}</div>
<div class="panel"><nav aria-label="Фильтр результатов">{{buttons}}</nav><div class="search"><label for="search">Найти сценарий или реплику</label>
<input id="search" type="search" placeholder="Например, серия или удаление"><p id="visible" class="meta" role="status" aria-live="polite"></p></div>
{{collection}}<div id="cases">{{rows}}</div><p id="empty" hidden>Подходящих сценариев нет.</p></div>
<footer>Проверка публичного API с настоящим LLM. Диалоги и ошибки относятся только к этому запуску. Отчёт работает без подключения к сети.</footer>
</main><script>
const cases = Array.from(document.querySelectorAll('.case'));
const search = document.querySelector('#search');
let filter = 'all';
function update() {
  const query = search.value.toLocaleLowerCase('ru');
  let count = 0;
  for (const row of cases) {
    row.hidden = (filter !== 'all' && row.dataset.status !== filter) || !row.textContent.toLocaleLowerCase('ru').includes(query);
    if (!row.hidden) count++;
  }
  document.querySelector('#visible').textContent = `Показано ${count} из ${cases.length}`;
  document.querySelector('#empty').hidden = count !== 0;
}
for (const button of document.querySelectorAll('[data-filter]')) {
  button.addEventListener('click', () => {
    filter = button.dataset.filter;
    for (const item of document.querySelectorAll('[data-filter]')) item.setAttribute('aria-pressed', String(item === button));
    update();
  });
}
search.addEventListener('input', update);
update();
</script></body></html>'''
