# YouDo Google Experience Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Постоянно сохранять опыт YouDo в закрытом Google Drive с реестром Sheets, небольшим локальным outbox и доказанным восстановлением после сбоев.

**Architecture:** Версионированные события пишутся в транзакционный SQLite outbox до внешних действий. Один синхронизатор публикует неизменяемые JSONL.gz-пакеты в Drive, проверяет скачанные байты, обновляет ключевые строки Sheets и облачный каталог, затем подтверждает локальные записи. Рабочий TTL-кэш и правила торгов остаются прежними.

**Tech Stack:** Существующий Python `/root/.hermes/hermes-agent/venv/bin/python`; stdlib sqlite3/json/gzip/hashlib/hmac/uuid/pathlib; уже установленные requests, google-auth, google-api-python-client, pytest и Playwright. Новые зависимости не устанавливать без доказанной необходимости. Корень проекта `/root/youdo-monitor`.

**Spec:** `docs/superpowers/specs/2026-10-15-youdo-google-experience-archive-design.md`, пользователь утвердил 2026-10-15 в Telegram.

## Global Constraints

- Основной архив — Google Drive; Google Sheets — индекс, VPS — буфер, не постоянная копия всех ТЗ.
- Лимит outbox + журналы + ожидающие пакеты: **64 MiB**; предупреждение при **75%**. Никакого удаления pending ради освобождения места.
- Порог дробления пакетов: **1 MiB несжатого JSONL**; длинное событие не обрезать. Если одна строка крупнее порога, упаковать отдельно при наличии резерва в общем лимите; это уточнение гранулярности, не разрешение превышать 64 MiB.
- UTC ISO-8601 для событий/каталогов; MSK для отображения человеку.
- Постоплата и существующие фильтры, цены, генератор текста, категории, расписание и ядро Hermes не меняются.
- Нет новых автооткликов при недоступной записи outbox или достижении лимита. Временный сбой Google при исправной очереди не требует остановки до лимита.
- Все внешние записи проверяются чтением ТОЧНОГО целевого объекта; HTTP success не заменяет readback.
- Не добавлять в Git cookies, токены, state, logs, приватные fixtures и выгрузки. Не печатать секреты в исключениях/тестах.
- Более трёх файлов в одной подзадаче запрещено: каждая из задач ниже имеет максимум три файла; коммиты выборочные.
- Не подключать автоматическую переписку и обучение моделей. Ручные исходы в Sheets поддерживаются.
- Тестовые данные синтетические и явно помечены; нельзя выдавать их за реальные данные YouDo.
- Сохранённые credentials использовать через существующую интеграцию; не создавать второй refresh-token store.

## Подтверждённое состояние на этапе планирования

- Git HEAD `1de5061`: только спецификация. Исходники и тесты пока untracked.
- Манифестов зависимостей в корне проекта не найдено; перечисленные Python-пакеты найдены importlib в runtime venv, но версии и исходный pytest baseline ещё не измерены.
- Существующий sentinel `/root/.hermes/scripts/google_auth_sentinel.py` содержит `GoogleAuthSentinel.refresh_workspace()`; `check_workspace()` сам делает refresh и НЕ является read-only.
- Read-only Drive GET существующим access token вернул **401**. Причина не установлена; Sheets не проверен. Сохранённый scopes list содержит Drive и Sheets; работоспособность этим не доказана.
- `runner.run_once()` делает ранний return при отсутствии новых заказов; новый sync нельзя ставить только в конец happy path.
- `OfferManager(dry_run=True)` возвращает success=True для симуляции: этот результат НЕ является отправленным откликом.
- Полные детали читаются только для fresh IDs. Бюджет клиента в текущем feed — строка; нельзя подменять его нашей ставкой или объявлять подтверждённым detail-бюджетом.

## Карта будущих файлов и контрактов

Новые модули — это проектируемые интерфейсы, а не якобы существующие API.

- `youdo_monitor/archive_events.py`: canonical events, allowlist, стабильные IDs, fingerprint правил.
- `youdo_monitor/archive_outbox.py`: durable SQLite queue, package ledger, лимиты, pending attempts.
- `youdo_monitor/archive_packages.py`: воспроизводимый JSONL.gz manifest, streaming verification.
- `youdo_monitor/archive_google.py`: авторизованный транспорт, Drive objects, readback и ограниченные retries.
- `youdo_monitor/archive_sheets.py`: keyed projections, ручные outcomes, восстановление индекса.
- `youdo_monitor/archive_sync.py`: state machine и cloud/local catalog.
- `youdo_monitor/archive_capture.py`: оркестрация записи наблюдений и offer guards, изоляция runner.
- `youdo_monitor/archive_legacy.py`: безопасный импорт исторического state.
- `youdo_monitor/archive_export.py`: псевдонимизированная выгрузка и предупреждение о residual PII.
- `youdo_monitor/archive_cli.py`: preflight/bootstrap/sync/import/lookup/export/verify/rebuild CLI.
- `youdo_monitor/runner.py`: единственный существующий production-модуль с изменением поведения.
- `tests/test_archive_*.py`: самостоятельные TDD-тесты по задачам ниже.
- `docs/research/2026-10-15-archive-verification.md`: сырые метрики и реальные handles без секретов.
- `data/archive_outbox.sqlite3`, `data/archive_catalog.json`, `data/archive_export.key`: private runtime only, Git ignored.

Общий объект события — `dict` с `schema_version=1`, `event_id: str`, `task_id: int`, `kind: str`, `observed_at: str`, `source: str`, `completeness: str`, `payload: dict`. Не вводить ORM/dataclass framework.

## Task 0: Защитить baseline и определить Google access gate

**Files (максимум 3 на шаг фиксации):** Create `.gitignore`; Create `docs/research/2026-10-15-archive-verification.md`. Исходные файлы фиксировать отдельными порциями по максимум три файла, не смешивать с feature diff.

- [ ] Проверить `git status --short`, `git diff`, список файлов в `youdo_monitor/` и `tests/`; прочитать все добавляемые файлы. Не использовать `git add .`.
- [ ] Создать `.gitignore`: `data/`, `logs/`, `__pycache__/`, `.pytest_cache/`, `*.pyc`, `.env`, `*.key`; не добавлять docs/research целиком (там есть приватные исследовательские данные).
- [ ] Проверить каждый исходный fixture на PII/secrets до Git; если fixture приватный — оставить вне Git и отметить baseline gap, не маскировать синтетикой.
- [ ] Прогнать baseline без verbose locals и без сети; сначала прочитать tests на реальные внешние действия. Зафиксировать версии и исходный отчёт:

```bash
PY=/root/.hermes/hermes-agent/venv/bin/python
$PY -c 'import sys,pytest,requests; print(sys.version); print("pytest",pytest.__version__); print("requests",requests.__version__)'
$PY -m pytest tests -q
```

- [ ] Git-коммиты baseline делать только явными путями и списком staged-файлов. Проверить `git show --stat` для каждой порции.
- [ ] Перед refresh загрузить `google-auth-sentinel` skill. Его реальная процедура приоритетнее предположений о CLI. Не вызывать `check_workspace(live_probe=False)` с ожиданием отсутствия записи: код делает refresh.
- [ ] Штатный refresh -> перечитать token -> Drive metadata GET -> Sheets metadata GET существующей доступной таблицы. Печатать только HTTP status, scopes и очищенную диагностику. Если refresh отклонён — STOP на авторизацию; не продолжать облачную часть.
- [ ] Не исправлять сторонний sentinel попутно. Новых облачных объектов в Task 0 нет.

**Acceptance:** baseline test counts и известные падения записаны; исходники защищены Git; доступ Google либо доказан read-only, либо оформлен блокер. Не считать весь проект готовым при блокере.

## Task 1: Канонические события и безопасное извлечение

**Files:** Create `youdo_monitor/archive_events.py`; Create `tests/test_archive_events.py`.

**Interfaces produced:**
`make_event(kind: str, task_id: int, payload: dict, *, observed_at: str, source: str, completeness: str, identity: str | None = None) -> dict`;
`task_payload(summary: dict, detail: dict | None) -> dict`;
`offer_payload(result: dict) -> dict`;
`rules_fingerprint(paths: list[Path]) -> str`.

- [ ] RED: одинаковое содержимое с разным observed_at имеет одинаковый event_id; смена ТЗ меняет ID; missing detail и API error не выдают complete; список профилей не проходит allowlist.

```python
from youdo_monitor.archive_events import make_event, task_payload, offer_payload

def test_content_identity_is_not_clock_identity():
    a = make_event('task_observed', 10, {'description':'abc'}, observed_at='2026-10-15T00:00:00Z', source='youdo', completeness='detail')
    b = make_event('task_observed', 10, {'description':'abc'}, observed_at='2026-10-15T01:00:00Z', source='youdo', completeness='detail')
    assert a['event_id'] == b['event_id']

def test_response_profiles_are_not_archived():
    safe = offer_payload({'success':True, 'response':{'ResultObject':{'Offer':{'Id':2, 'CreatorInfo':{'Phone':'SECRET_PHONE'}}}}})
    assert safe['offer_id'] == 2
    assert 'SECRET_PHONE' not in str(safe)
```

- [ ] Run `$PY -m pytest tests/test_archive_events.py -q`, увидеть целевое падение.
- [ ] GREEN: canonical UTF-8 JSON (`ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False`), SHA-256 по schema/kind/task/source/completeness/payload; observed_at исключить. offer identity включает attempt_id. Поля внешних ошибок — только безопасные enum/code без raw request.
- [ ] Проверить явные null, длинное ТЗ, имя создателя/профиль исключены, fingerprint включает filters.py и влияющие config-значения; хранить code revision отдельно.
- [ ] Run tests -> выборочный commit `feat: define safe archive events`.

## Task 2: Durable outbox, лимит и запрет повторных неоднозначных действий

**Files:** Create `youdo_monitor/archive_outbox.py`; Create `tests/test_archive_outbox.py`.

**Consumes:** события Task 1.
**Produces:** `ArchiveBlocked(RuntimeError)`; `Outbox(path: Path, *, max_bytes: int=67108864)`; методы `enqueue(event: dict)->bool`, `pending()->list[dict]`, `prepare(batch_id: str, event_ids: list[str], metadata: dict)->None`, `batches()->list[dict]`, `advance(batch_id: str, stage: str, metadata: dict)->None`, `ack(batch_id: str)->None`, `usage()->dict`, `has_unresolved_attempt(task_id: int)->bool`.

- [ ] RED: после close/reopen событие сохраняется, повтор event_id не добавляется; ack невозможен до catalog_verified.

```python
import pytest
from youdo_monitor.archive_outbox import Outbox, ArchiveBlocked

def test_no_ack_before_cloud_verification(tmp_path):
    q = Outbox(tmp_path/'out.sqlite3')
    q.enqueue({'event_id':'e1','task_id':1,'kind':'task_observed','payload':{},'schema_version':1,'observed_at':'2026-10-15T00:00:00Z','source':'test','completeness':'partial'})
    q.prepare('b1', ['e1'], {})
    with pytest.raises(ArchiveBlocked):
        q.ack('b1')
    assert len(q.pending()) == 1
```

- [ ] Run failing tests -> GREEN: schema events, batches, batch_events, unresolved attempts; SQLite foreign keys + транзакции; 0600 файлы/0700 staging. Работающие unknown attempts не исчезают при очистке payload.
- [ ] Реализовать строгий budget не только по размерам строк: sum DB/journal/staging, SQLite page cap, консервативное резервирование transaction/journal/package bytes ДО записи. Отдельно тестировать peak usage. Если лимит невозможно гарантировать выбранным journal mode — STOP, сменить режим, не ослаблять критерий.
- [ ] Test 75% warning, oversized rejected без обрезки, disk error, retry после process interruption, monotonic stages; уменьшенный тестовый лимит должен вместить SQLite metadata, иначе проверяется не тот путь.
- [ ] Для неизвестного результата попытки сохранить durable блок повторного автоотклика; после успешного API даже ошибка записи result не должна разрешать повтор в следующем run. Не делать retry YouDo.
- [ ] Run tests -> commit `feat: add bounded durable archive outbox`.

## Task 3: Детерминированные пакеты и проверка байтов

**Files:** Create `youdo_monitor/archive_packages.py`; Create `tests/test_archive_packages.py`.

**Produces:** `build_package(batch_id: str, events: list[dict])->tuple[bytes,dict]`; `verify_package(blob: bytes, *, expected_sha256: str, expected_count: int)->list[dict]`; `partition_events(events: list[dict], target_bytes: int=1048576)->list[list[dict]]`.

- [ ] RED: одинаковые события и batch_id дают идентичные gzip bytes, неверный hash/count блокируют verify, partition не теряет event_id.

```python
import hashlib
from youdo_monitor.archive_packages import build_package, verify_package

def test_package_is_reproducible():
    events = [{'event_id':'a','task_id':1,'kind':'task_observed'}]
    a, meta = build_package('b1', events)
    b, _ = build_package('b1', events)
    assert a == b
    assert verify_package(a, expected_sha256=hashlib.sha256(a).hexdigest(), expected_count=1) == events
```

- [ ] Run RED -> GREEN: stable event order, first line manifest `{schema_version,batch_id,event_count}`, `gzip.compress(..., mtime=0)`; manifest не входит в event_count. SHA-256 сжатых байтов, timestamps manifest не зависят от текущего clock.
- [ ] Test кириллица, строки с переводами строк (JSON escaping), empty batch forbidden, single oversized event отдельно с проверкой budget outbox; один batch_id никогда не переиспользовать с иным payload.
- [ ] Run tests -> commit `feat: package and verify archive batches`.

## Task 4: Google transport и идемпотентные Drive objects

**Files:** Create `youdo_monitor/archive_google.py`; Create `tests/test_archive_google.py`.

**Produces:** `GoogleArchive(transport, token_path: Path)` с `preflight()->dict`, `ensure_private_root(archive_key: str)->dict`, `reserve_file_id()->str`, `put_verified(file_id: str, parent_id: str, name: str, blob: bytes, properties: dict)->dict`, `download(file_id: str)->bytes`, `get_json(file_id: str)->dict`, `put_json_verified(file_id: str, data: dict)->dict`, `permissions(file_id: str)->list[dict]`.

- [ ] RED fake transport tests: upload timeout после server commit -> retry читает ТУ ЖЕ file_id и не создаёт вторую копию; wrong bytes -> error; 401 не замаскирован; неожиданные permissions блокируют bootstrap.

```python
from unittest.mock import Mock
from youdo_monitor.archive_google import GoogleArchive

def test_download_uses_exact_id(tmp_path):
    transport = Mock()
    transport.get_bytes.return_value = b'archive'
    g = GoogleArchive(transport, tmp_path/'token.json')
    assert g.download('file-123') == b'archive'
    transport.get_bytes.assert_called_once_with('https://www.googleapis.com/drive/v3/files/file-123', params={'alt':'media'})
```

- [ ] Реализовать injected HTTP transport в том же модуле (read token при запросе, bounded timeout, redacted errors). Refresh только штатным механизмом, не создавать новый credential store; 401 после одной штатной refresh-попытки — auth blocker. Runtime refresh не вызывать на каждый API request.
- [ ] До реализации сверить официальные Drive files.generateIds/files.create/permissions API. Для обычного JSON/gzip и папок использовать предварительно выделенный file_id, сохранённый в ledger ДО create. appProperties `{archive_key,batch_id,schema_version}` — дополнительный lookup, но не доказательство уникальности имени.
- [ ] Bootstrap spreadsheet: Google документирует, что pre-generated IDs не поддерживаются для Google Workspace files, кроме folder/drive-sdk (официальный manage-uploads/create-file проверен при планировании). Не тратить probe на заведомо неподдерживаемый spreadsheet ID. Создать таблицу через Drive files.create с MIME spreadsheet, уникальным archive_key в appProperties и parent root; ID сохранить сразу. Durable bootstrap intent создаётся ДО POST. При неизвестном результате не повторять POST автоматически: искать по archive_key в точном root; одна найденная таблица — принять и прочитать; ноль/несколько — bootstrap blocker для разбирательства, а не новая таблица. Затем Sheets batchUpdate листов и exact readback. Обычные пакеты/папки используют pre-generated ID по отдельному проверенному пути.
- [ ] `put_verified`: create если точный ID отсутствует; если уже есть — скачать и сравнить, mismatch ошибка, не перезаписывать immutable batch. Для catalog update — exact-ID PATCH + content readback.
- [ ] Retry: максимум 3 попытки суммарно на операцию для 429/5xx, delays 1/2 s плюс jitter, Retry-After с верхним пределом 30 s. Network ambiguity readback перед повтором create. Не повторять 400/403 вслепую.
- [ ] Права проверять по owner permission созданных root и sheet; anyone/domain/unexpected user/group -> blocker. Не менять чужие permissions.
- [ ] Run tests -> commit `feat: add verified private Google archive transport`.

## Task 5: Sheets projections и ручные outcomes

**Files:** Create `youdo_monitor/archive_sheets.py`; Create `tests/test_archive_sheets.py`.

**Consumes:** Google transport Task 4; события Task 1.
**Produces:** `SheetsIndex(google, spreadsheet_id: str)`; `ensure_tabs()->dict`, `read_rows(tab: str)->list[dict]`, `upsert(tab: str, rows: list[dict], *, key: str, owned_columns: list[str])->None`, `apply_events(events: list[dict], package: dict)->None`, `read_outcomes()->list[dict]`, `rebuild(events: list[dict])->None`.

- [ ] RED test формулы, перестановки строк, повторных записей и ручных полей.

```python
from unittest.mock import Mock
from youdo_monitor.archive_sheets import SheetsIndex

def test_manual_fields_are_not_owned():
    g = Mock()
    s = SheetsIndex(g, 'sheet-1')
    assert 'примечание' not in s.ORDER_MACHINE_COLUMNS
    assert 'task_id' in s.ORDER_MACHINE_COLUMNS
```

- [ ] Run RED -> GREEN: листы `Заказы`, `Отклики`, `Результаты`, `Синхронизация`; фиксированные headers; все writes `valueInputOption='RAW'`. Значения возвращать и проверять по ключам, а не по известному старому номеру строки.
- [ ] В machine columns заказов включить observation_id, rules_fingerprint, first_seen, last_seen и ссылки на все пакетные ревизии. last_seen обновляется и при отсутствии нового content-события.
- [ ] На restart синхронизатор читает keyed projections и pending batch keys с удалённой стороны; не накапливать бесконечную локальную таблицу всех task payload для dedup. Для состояния dedup нужного active feed получать последний remote fingerprint; повторную старую версию контента искать по event_id в связанных пакетах при необходимости.
- [ ] Результаты: machine outcome_id назначается один раз; revision — SHA-256 канонических ручных полей. Пользовательские поля type/task/time/amount/currency/source/note не перезаписывать. Отсутствующий outcome_id присваивать и перечитывать перед импортом. Неверный task_id/type/amount/time — error column, не outcome.
- [ ] Test append ambiguity: reread keys перед retry; no duplicates. Test user row edit -> revision event; row deletion не удаляет cloud event. Duplicate outcome_id -> conflict alert.
- [ ] После updates перечитать только точные диапазоны и сравнить expected machine cells. При конфликте человеческого редактирования не перезаписывать целую строку.
- [ ] Run tests -> commit `feat: maintain keyed Sheets archive index`.

## Task 6: Синхронизация, каталог и crash recovery

**Files:** Create `youdo_monitor/archive_sync.py`; Create `tests/test_archive_sync.py`.

**Consumes:** Outbox, packages, GoogleArchive, SheetsIndex.
**Produces:** `ArchiveSync(outbox, google, sheets, catalog_path: Path)`; `run()->dict`, `lookup(task_id: int)->list[dict]`, `rebuild_to(spreadsheet_id: str)->dict`.

- [ ] RED: parametrized failures after prepare/upload/download/sheets-write/catalog-write не удаляют pending до полного подтверждения.

```python
import pytest
from youdo_monitor.archive_outbox import ArchiveBlocked

@pytest.mark.parametrize('stage', ['package_prepared','drive_verified','sheets_verified'])
def test_partial_stage_cannot_release_payload(stage, prepared_outbox):
    q, batch_id = prepared_outbox
    if stage != 'package_prepared':
        q.advance(batch_id, 'drive_verified', {'file_id':'f1'})
    if stage == 'sheets_verified':
        q.advance(batch_id, 'sheets_verified', {})
    with pytest.raises(ArchiveBlocked):
        q.ack(batch_id)
    assert q.pending()
```

`prepared_outbox` fixture создаётся в этом тестовом файле из Outbox: enqueue event -> prepare batch.

- [ ] Run RED -> GREEN: читать manual outcomes -> enqueue revisions -> сформировать package -> сохранить IDs/stage -> upload -> download/hash/count -> keyed sheet readback -> exact cloud catalog readback -> local atomic catalog update -> acknowledge.
- [ ] Catalog не содержит токенов/PII; содержит root_folder_id, spreadsheet_id, sheet_ids, catalog_file_id, schema_version, last_verified_sync, batch registry. Cloud registry растёт в Drive, не копировать целиком на VPS навсегда; локально сохранять только locator/checkpoint и незавершённые операции. Для большого каталога поддержать streaming manifest read, не грузить весь архив в RAM.
- [ ] Batch registry в catalog_verified не утверждает будущий локальный ack. Внешний статус `verified` означает подтверждённые cloud bytes + sheet; локальный ack — отдельное состояние.
- [ ] После ack освобождать SQLite pages для повторного использования; при high-water reclaim выполнить безопасный incremental vacuum вне сетевой транзакции. Не оставлять файл на лимите при нуле pending так, чтобы навсегда блокировались новые события. Никакого удаления облачных пакетов.
- [ ] Recover missing local catalog по сохранённому root/catalog locator; одновременно потерянные все locators — явный ручной recovery blocker. lookup проверяет hashes и ищет task_id по Sheets, затем пакетам.
- [ ] Test restart каждого этапа, corrupt download, wrong count, manual edit, cloud missing object, duplicate keys, no-new-events sync, восстановление machine Sheets из пакетов в ДРУГУЮ таблицу.
- [ ] Run tests -> commit `feat: recover archive synchronization safely`.

## Task 7: Capture API и безопасный runner hook

Две подзадачи, не более трёх файлов каждая.

### 7A: Capture adapter

**Files:** Create `youdo_monitor/archive_capture.py`; Create `tests/test_archive_capture.py`.

**Produces:** `ArchiveCapture(outbox, sync)`; `observe_feed(feed: list[dict])->None`, `observe_detail(summary: dict, detail: dict, decision)->None`, `begin_offer(task_id: int, price: int, pitch: str)->str`, `finish_offer(attempt_id: str, result: dict)->None`, `sync_now()->dict`, `can_offer(task_id: int)->bool`.

- [ ] RED: begin_offer writes pending before returning; unresolved attempt блокирует can_offer; classification event ссылается на детальное observation; detail error не становится complete.

```python
from unittest.mock import Mock
from youdo_monitor.archive_capture import ArchiveCapture

def test_begin_offer_is_durable_before_return():
    q, sync = Mock(), Mock()
    q.has_unresolved_attempt.return_value = False
    c = ArchiveCapture(q, sync)
    attempt_id = c.begin_offer(1, 1000, 'полный текст')
    assert attempt_id
    assert q.enqueue.called
    assert q.enqueue.call_args.args[0]['kind'] == 'offer_attempt'
```

- [ ] Run RED -> GREEN; исключения архива наружу как ArchiveBlocked; не вызывать OfferManager или Telegram внутри capture.
- [ ] Test sanitized failure, unknown response, full pitch, dry-run отдельный source/не создаёт реальные offer events, last_seen без content duplication.
- [ ] Run tests -> commit `feat: capture monitoring experience without side effects`.

### 7B: Runner integration

**Files:** Modify `youdo_monitor/runner.py:51-245`; Create `tests/test_archive_runner.py`; Modify `tests/test_runner_auto_offer.py` (явно inject private archive mock).

**Interface change:** `run_once(..., archive: object | None = None)` — production default constructs archive из private catalog; tests inject fake. До активации runtime config feature disabled; после успешного bootstrap и verification явно active=true. Невозможность инициализировать enabled архив запрещает автоотклик, но позволяет безопасный read-only отчёт.

- [ ] RED test exact ordering and adverse paths:

```python
def test_offer_call_is_preceded_by_persisted_attempt(runner_harness):
    h = runner_harness
    h.run()
    assert h.calls.index('begin_offer') < h.calls.index('submit_postpaid_offer')
    assert h.calls.index('submit_postpaid_offer') < h.calls.index('finish_offer')

def test_archive_block_prevents_offer(runner_harness):
    h = runner_harness
    h.archive.begin_offer.side_effect = h.ArchiveBlocked('full')
    h.run()
    h.offer.submit_postpaid_offer.assert_not_called()
```

`runner_harness` в этом файле монтирует DummyClient/Notifier/OfferManager и isolated state; никаких real external actions.

- [ ] Run RED -> GREEN: observe_feed immediately after feed, детали с decision внутри fresh loop, begin_offer перед submit, finish_offer после. Не сохранять simulate success как real submitted.
- [ ] Sync вызвать также при fresh=[], API exception и нормальном выходе, ограниченный budget времени; browser start fail не должен отменять уже существующий pending sync. Не ставить всё только в finally client.close без проверки ранее созданных объектов.
- [ ] Сохранить существующий порядок/условия отправки торгов за исключением новых archive safety guards. Если result persistence падает после реального успеха, сохранить текущий state success если возможно и durable unresolved ledger не снимается до reconciliation.
- [ ] Stats: archive_pending, archive_bytes, archive_synced_events, archive_warning, archive_errors, auto_offers_blocked_archive, archive_last_verified_sync. Публичный статус без payload.
- [ ] Tests early return, all-seen feed, unauth, dry-run, no-auto-offer, no-new, archive init error, Google offline below limit, exact limit, offer API exception и finish failure.
- [ ] Run all tests -> compare baseline -> commit `feat: wire guarded experience capture into monitor`.

## Task 8: Исторический импорт без утечки API profiles

**Files:** Create `youdo_monitor/archive_legacy.py`; Create `tests/test_archive_legacy.py`.

**Produces:** `import_state(path: Path, outbox)->dict` (read-only input).

- [ ] RED: повторный импорт даёт ноль новых событий; input SHA неизменен; полный текст из ResultObject.Offer.Description приоритетнее обрезанного extra.pitch; при отсутствии полного текста не объявлять восстановленным.

```python
def test_legacy_import_keeps_source_unchanged(tmp_path, legacy_state, outbox):
    import hashlib, json
    from youdo_monitor.archive_legacy import import_state
    p = tmp_path/'state.json'
    p.write_text(json.dumps(legacy_state), encoding='utf-8')
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    import_state(p, outbox)
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before
    assert import_state(p, outbox)['inserted'] == 0
```

`legacy_state` fixture этого файла синтетический: title, rejected status, extra.pitch='short', result.response.ResultObject.Offer.Description='full pitch', profiles с маркером PRIVATE; никаких настоящих контактов.

- [ ] Run RED -> GREEN: source=legacy_state, completeness=partial, stable attempt identity `legacy:<task_id>:<offer_id-or-seen_at>`, missing budget/TЗ/outcome=null. offered status без подтверждённого result не равен API-confirmed success.
- [ ] Test PRIVATE absent из всех событий, input corrupted -> explicit error, timestamp conversion UTC, long pitch exact match.
- [ ] Run tests -> commit `feat: import incomplete historical monitoring state`.

## Task 9: Выгрузка для обучения

**Files:** Create `youdo_monitor/archive_export.py`; Create `tests/test_archive_export.py`.

**Produces:** `redact_text(text: str)->str`; `export_events(events, output: Path, *, key: bytes)->dict`.

- [ ] RED: task/offer/attempt/user IDs не остаются в экспортируемых полях, HMAC стабилен и связывает события, raw contacts/tokens убраны; непонятные косвенные признаки явно не обещать устранёнными.

```python
from youdo_monitor.archive_export import redact_text

def test_known_direct_contacts_are_removed():
    s = redact_text('email demo@example.invalid; tel +7 999 000-11-22; https://example.invalid/contact')
    assert 'demo@example.invalid' not in s
    assert '000-11-22' not in s
    assert 'https://' not in s
```

- [ ] Run RED -> GREEN: allowlist output fields, HMAC SHA256 с persistent 0600 export key в data/archive_export.key; key не помещать в catalog/Drive/Git. При потере ключа не смешивать новую псевдонимизацию со старой молча.
- [ ] Фикстуры: кириллица, email/телефон нескольких форматов, URL, bearer/API keys, имена из структурированных полей, вложенный профиль. Не заявлять гарантированную анонимность произвольного free text.
- [ ] Потоковое чтение проверенных пакетов, bounded temporary output, verified upload в закрытый exports. Manifest фиксирует privacy limitations и test coverage; никакой автоматической передачи тренеру/модели.
- [ ] Run tests -> commit `feat: export pseudonymized experience datasets`.

## Task 10: CLI и безопасный реальный end-to-end

**Files:** Create `youdo_monitor/archive_cli.py`; Create `tests/test_archive_cli.py`; Update `docs/research/2026-10-15-archive-verification.md`.

**CLI produced:**
`python -m youdo_monitor.archive_cli preflight`
`bootstrap --name 'YouDo Experience Archive'`
`import-state --path data/state.json`
`sync`
`lookup --task-id 15320824`
`export --output <private-path>`
`verify --live-feed --no-offers --no-notifications`
`rebuild-index --destination-spreadsheet-id <explicit-test-id>`.

- [ ] RED: CLI without subcommand/required explicit destructive-like target refuses; verify flags never instantiate OfferManager.submit or Telegram send; no implicit rebuild to production.

```python
from youdo_monitor.archive_cli import build_parser

def test_verify_has_explicit_no_side_effect_flags():
    a = build_parser().parse_args(['verify','--live-feed','--no-offers','--no-notifications'])
    assert a.no_offers and a.no_notifications
```

- [ ] Run RED -> GREEN: bootstrap durable intent/IDs, permissions readback, exact local + cloud catalog readback. Все созданные объекты входят в отчёт; тестовые ресурсы не удалять без отдельного согласия.
- [ ] Bootstrap permissions и reserve ID обычных файлов фактически проверить до production bootstrap; для spreadsheet использовать Task 4 без pre-generated ID. При неоднозначном create остановить bootstrap и проверить exact archive_key; не выполнять второй create ради успешного отчёта.
- [ ] Реальный import существующего state -> sync -> readback hashes/counts -> повтор import/sync даёт ноль новых immutable packets для неизменных данных.
- [ ] Live feed through existing YouDoClient/runtime под scrubbed proxy; детали новых задач, всё без офферов и уведомлений. Отдельный archive CLI verify вместо run_once(dry_run=True), который пишет seen-state.
- [ ] Реально изменить один ручной outcome в тестовом реестре -> импорт -> изменить примечание -> вторая revision; проверить original user fields сохранены. Тестовые события source=integration_test, из production training export исключены по умолчанию.
- [ ] Download corruption/retry проверять на fake transport, не портить production Drive. Real rebuild в отдельный test spreadsheet -> сравнение machine row IDs/count/content с проверенными пакетами.
- [ ] Метрики `/usr/bin/time -v` (если присутствует, иначе resource.getrusage): elapsed, maxRSS, outbox bytes, pending count, compressed/uncompressed bytes, downloaded hash, sheet row counts и число API calls. Не писать ожидаемые числа как измеренные.
- [ ] Run full tests and real verification -> commit `feat: expose and verify archive operations`.

## Task 11: Review gate, активация и навигация

**Files:** Update verification report; Update skill `freelance-order-monitoring/SKILL.md`; Create `freelance-order-monitoring/references/experience-archive.md` через skill_manage после загрузки writing-skills/соответствующего skill protocol, если требуется. Навигационные изменения не расходятся с реальными IDs из каталога.

- [ ] Независимый reviewer получает spec, plan, diff и реальные test outputs. Проверяет data loss, idempotency после ambiguous writes, unbounded local growth, private permissions, plaintext export risk, timestamps, manual fields, dry-run side effects. Review не должен иметь production credentials.
- [ ] Каждый реальный defect -> regression RED -> минимальный fix -> GREEN; два провала одним методом -> сменить источник, не третий слепой повтор.
- [ ] Пробник recovery после interrupt и safe live verify должны быть зелёными до active=true. Активация только private runtime catalog, existing cron unchanged; feature flag false прекращает capture/new archive work, но не удаляет queue/cloud objects. Пока feature active, отсутствие outbox блокирует новые офферы.
- [ ] После активации проверить первый обычный cron run read-only по логам/state/cloud exact objects. Не запускать вручную второй боевой run ради доказательства оффера. Если пришлось ждать следующего расписания — честно отметить ещё не проверенный пункт.
- [ ] В скилле сохранить точный local catalog path и lookup CLI, не список заказов. Документировать recovery/auth/unknown offers и статус ручных исходов.
- [ ] Финальный отчёт: реальные root folder/spreadsheet IDs, local catalog path, counts baseline/after, именованные ошибки/падения, bytes/RSS/time, число imported/partial/unknown outcomes, результаты повторного sync, rebuild и actual scheduled run. Никакого «готово» при блокере или неисполненном criterion.
- [ ] Последний commit и `git diff --check`, `git status --short`; отдельно показать унаследованные untracked и собственные изменения.

## Матрица покрытия спецификации

- §1, §2 scope, legacy runner, untouched trading: Task 0, 7, 11.
- §3 events/identity/unknown outcomes/rules version: Task 1, 2, 5, 7.
- §4 bounded outbox/fail-safe: Task 2, 3, 7; peak disk тест Task 2, real measurements Task 10.
- §5 immutable Drive + verify/retries: Task 3, 4, 6, 10.
- §6 Sheets/manual edits/RAW/key dedup: Task 5, 6, 10.
- §7 catalog/lookup/rebuild/skill navigation: Task 6, 10, 11.
- §8 credentials/privacy/export: Task 0, 1, 4, 9.
- §9 historical import: Task 8, 10.
- §10 acceptance/tests/metrics/review: RED/GREEN всех задач, full tests Task 10, review Task 11.
- §11 staged implementation: отдельные task commits, no-agent core edits, по максимум три файла на подзадачу.

## Approval and Execution Handoff

План не является выполненной реализацией. До одобрения пользователем не создавать облачные ресурсы и не править боевой runner.

Вариант 1 (рекомендуется): Subagent-Driven — свежий implementer на независимую подзадачу, последовательная интеграция общих контрактов, двухэтапное ревью spec/correctness. Секреты и live side effects остаются родительскому агенту.
Вариант 2: Inline Execution — executing-plans в текущей сессии с контрольными точками. При блокере остановить затронутую часть, не подменять настоящие результаты моками.

Официальная документация для реализации (не evidence работоспособности аккаунта):
- Google Drive: manage uploads, custom properties, files.generateIds, files.create, permissions.list.
- Google Sheets: spreadsheets.values.batchUpdate (RAW), usage limits, spreadsheets.batchUpdate.
Точные актуальные схемы читать перед coding; загруженная документация не отменяет live probes.
