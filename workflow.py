"""Telegram task cards, employee navigation, deadline requests and reminders."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta

from bot import APIError, CompletionFlow, FINISH_BUTTON, LOG, MOSCOW, TASK_HEADERS, find_task, text_parts, yes

CLOSED = ('Выполнена', 'Отменена')
PAGE_SIZE = 6
MY = '📋 Мои задачи'
OVERVIEW = '📊 Обзор задач'
REVIEW = '📥 На проверке'
PEOPLE = '👥 Сотрудники'
MOVE = '📅 Запросить перенос срока'


def menu(admin):
    rows = [[OVERVIEW, REVIEW], [PEOPLE, '📊 Открыть таблицу'], [MY, FINISH_BUTTON], [MOVE]] if admin else [[MY], [FINISH_BUTTON], [MOVE]]
    return {'keyboard': [[{'text': text} for text in row] for row in rows],
            'resize_keyboard': True, 'is_persistent': True}


def token(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:32]


def revision(task):
    return token(json.dumps([task.get(k, '') for k in ('Задача', 'ФИО', 'ID сотрудника', 'Дедлайн')], ensure_ascii=False))


def deadline(value):
    value = str(value).strip()
    if not value:
        return None
    for fmt in ('%d.%m.%Y %H:%M', '%d.%m.%Y %H:%M:%S', '%d.%m.%Y', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            result = datetime.strptime(value, fmt).replace(tzinfo=MOSCOW)
            if '%H' not in fmt:
                result = result.replace(hour=23, minute=59)
            return result
        except ValueError:
            pass
    return None


def input_deadline(value):
    try:
        return datetime.strptime(value.strip(), '%d.%m.%Y %H:%M').replace(tzinfo=MOSCOW)
    except ValueError:
        raise ValueError('Введите дату и время: ДД.ММ.ГГГГ ЧЧ:ММ, по Москве. Например: 10.10.2026 18:00.') from None


def serial(value):
    return (value.replace(tzinfo=None) - datetime(1899, 12, 30)).total_seconds() / 86400


def button(text, data):
    return {'text': text[:80], 'callback_data': data}


def keyboard(rows):
    return {'inline_keyboard': rows}


class Workflow:
    def __init__(self, store, telegram, sheets, admins):
        self.store, self.tg, self.sheets, self.admins = store, telegram, sheets, admins
        self.db = store.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS ui_dialogs(chat INTEGER PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS task_acceptances(task TEXT PRIMARY KEY, employee TEXT NOT NULL, revision TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS deadline_requests(id TEXT PRIMARY KEY, task TEXT NOT NULL,
            employee TEXT NOT NULL, chat INTEGER NOT NULL, original TEXT NOT NULL,
            proposed TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
          CREATE TABLE IF NOT EXISTS task_mutations(id TEXT PRIMARY KEY, task TEXT NOT NULL,
            expected TEXT NOT NULL, changes TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            source TEXT NOT NULL DEFAULT '', author INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS ui_outbox(id TEXT PRIMARY KEY, chat INTEGER NOT NULL,
            text TEXT NOT NULL, markup TEXT, state TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            guard TEXT NOT NULL DEFAULT '{}');
          CREATE TABLE IF NOT EXISTS observed_tasks(task TEXT PRIMARY KEY, payload TEXT NOT NULL);
        ''')

    def send(self, uid, text, markup=None):
        parts = text_parts(text)
        for i, part in enumerate(parts):
            self.tg.send(uid, part, reply_markup=markup if i == len(parts) - 1 else None)

    def queue(self, key, uid, text, markup=None, guard=None):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO ui_outbox(id,chat,text,markup,guard) VALUES(?,?,?,?,?)',
                (key, uid, text, json.dumps(markup) if markup else None, json.dumps(guard or {})))

    def flush(self):
        snapshot = None
        reminder_mode = self.db.execute("SELECT value FROM settings WHERE key='reminders_enabled'").fetchone()
        for entry in self.db.execute("SELECT * FROM ui_outbox WHERE state='pending' AND retry_at<=? ORDER BY rowid LIMIT 50", (time.time(),)).fetchall():
            guard = json.loads(entry['guard'])
            if guard.get('reminder') and reminder_mode and reminder_mode[0] == '0':
                continue
            if guard.get('employee') and self.store.chat(guard['employee']) != entry['chat']:
                with self.db:
                    self.db.execute("UPDATE ui_outbox SET state='obsolete' WHERE id=?", (entry['id'],))
                continue
            if guard.get('admin') and entry['chat'] not in self.admins:
                continue
            if guard.get('task'):
                if snapshot is None:
                    snapshot = self.sheets.snapshot()[0]
                try:
                    _, task = find_task(snapshot, guard['task'])
                    obsolete = revision(task) != guard['revision'] or task['Статус задачи'] in CLOSED
                    if guard.get('reminder'):
                        obsolete = obsolete or not yes(task['Готова к отправке']) or bool(self.pending_report(guard['task']))
                    if obsolete:
                        with self.db:
                            self.db.execute("UPDATE ui_outbox SET state='obsolete' WHERE id=?", (entry['id'],))
                        continue
                except ValueError:
                    with self.db:
                        self.db.execute("UPDATE ui_outbox SET state='obsolete' WHERE id=?", (entry['id'],))
                    continue
            try:
                parts = text_parts(entry['text'])
                markup = json.loads(entry['markup']) if entry['markup'] else None
                for index in range(entry['progress'], len(parts)):
                    self.tg.send(entry['chat'], parts[index], reply_markup=markup if index == len(parts) - 1 else None)
                    with self.db:
                        self.db.execute('UPDATE ui_outbox SET progress=? WHERE id=?', (index + 1, entry['id']))
                with self.db:
                    self.db.execute("UPDATE ui_outbox SET state='sent' WHERE id=?", (entry['id'],))
            except APIError as exc:
                if exc.kind == 'fatal':
                    raise
                with self.db:
                    self.db.execute('UPDATE ui_outbox SET retry_at=? WHERE id=?', (time.time() + max(60, exc.retry_after), entry['id']))
                LOG.warning('Уведомление интерфейса не отправлено; повтор позже')

    def dialog(self, uid, data=None):
        if data is not None:
            data = dict(data)
            data.setdefault('nonce', secrets.token_hex(8))
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO ui_dialogs VALUES(?,?,?)', (uid, json.dumps(data, ensure_ascii=False), time.time() + 3600))
            return data
        row = self.db.execute('SELECT data FROM ui_dialogs WHERE chat=? AND expires>?', (uid, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def clear(self, uid):
        with self.db:
            self.db.execute('DELETE FROM ui_dialogs WHERE chat=?', (uid,))
            self.db.execute('DELETE FROM settings WHERE key=?', (f'finish:{uid}',))

    def admin(self, uid):
        if uid not in self.admins:
            raise ValueError('Доступно только администратору.')

    def owned(self, uid, task, people, open_only=True):
        employee = str(task['ID сотрудника']).strip()
        person = people.get(employee)
        if (self.store.chat(employee) != uid or not person or not yes(person['Активен'])
                or str(person['ФИО']).strip() != str(task['ФИО']).strip()):
            raise ValueError('Можно работать только со своими активными задачами.')
        if open_only and task['Статус задачи'] in CLOSED:
            raise ValueError('Задача уже выполнена или отменена.')

    def resolve(self, tasks, digest):
        keys = [str(t['Номер задачи']).strip() for _, t in tasks if token(str(t['Номер задачи']).strip()) == digest]
        if len(keys) != 1:
            raise ValueError('Задача не найдена или номер повторяется.')
        return find_task(tasks, keys[0])

    def person(self, people, digest):
        matches = [key for key in people if token(key) == digest]
        if len(matches) != 1:
            raise ValueError('Сотрудник не найден.')
        return matches[0]

    def page(self, items, page):
        page = max(0, min(int(page), max(0, (len(items) - 1) // PAGE_SIZE)))
        return page, items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    def navigation(self, size, page, prefix):
        buttons = []
        if page:
            buttons.append(button('← Назад', f'{prefix}:{page-1}'))
        if (page + 1) * PAGE_SIZE < size:
            buttons.append(button('Далее →', f'{prefix}:{page+1}'))
        return [buttons] if buttons else []

    def employees(self, uid, page=0):
        self.admin(uid)
        _, people = self.sheets.snapshot()
        all_people = sorted(people.items(), key=lambda item: (str(item[1]['ФИО']).casefold(), item[0]))
        page, subset = self.page(all_people, page)
        lines, rows = [], []
        for key, person in subset:
            label = str(person['ФИО']) + ('' if yes(person['Активен']) else ' (неактивен)')
            lines.append(f'{label} — ID сотрудника: {key}')
            rows.append([button(label, 'staff:' + token(key) + ':0')])
        rows += self.navigation(len(all_people), page, 'people')
        self.send(uid, (f'Сотрудники: {len(all_people)}. Страница {page+1}\n\n' + '\n'.join(lines)) if lines else 'Список сотрудников пока пуст.', keyboard(rows))

    def pending_report(self, key):
        return self.db.execute("SELECT * FROM completions WHERE task=? AND state IN ('pending','approving','returning') ORDER BY rowid DESC LIMIT 1", (key,)).fetchone()

    def task_list(self, uid, category='mine', employee=None, page=0):
        if category != 'mine':
            self.admin(uid)
        tasks, people = self.sheets.snapshot()
        if category == 'mine':
            binding = self.db.execute('SELECT employee FROM bindings WHERE chat=?', (uid,)).fetchone()
            if not binding or binding[0] not in people or not yes(people[binding[0]]['Активен']):
                raise ValueError('Сначала подключитесь по приглашению администратора.')
            employee = binding[0]
        now = datetime.now(MOSCOW)
        selected = []
        for _, task in tasks:
            if employee is not None and str(task['ID сотрудника']).strip() != employee:
                continue
            if category == 'mine' and task['Статус задачи'] in CLOSED:
                continue
            due = deadline(task['Дедлайн'])
            if category == 'overdue' and (task['Статус задачи'] in CLOSED or not due or due >= now):
                continue
            if category == 'nodue' and (task['Статус задачи'] in CLOSED or str(task['Дедлайн']).strip()):
                continue
            if category == 'open' and task['Статус задачи'] in CLOSED:
                continue
            if category == 'review' and not self.pending_report(str(task['Номер задачи']).strip()):
                continue
            if category == 'requests' and not self.db.execute("SELECT 1 FROM deadline_requests WHERE task=? AND state='pending'", (str(task['Номер задачи']).strip(),)).fetchone():
                continue
            selected.append(task)
        selected.sort(key=lambda t: (t['Статус задачи'] in CLOSED, deadline(t['Дедлайн']) or datetime.max.replace(tzinfo=MOSCOW), str(t['Номер задачи'])))
        page, subset = self.page(selected, page)
        title = {'mine': 'Мои открытые задачи', 'review': 'Задачи на проверке', 'requests': 'Запросы переноса срока', 'overdue': 'Просроченные задачи', 'nodue': 'Задачи без срока', 'open': 'Открытые задачи', 'staff': 'Задачи сотрудника'}.get(category, 'Задачи')
        if category == 'staff':
            title += ': ' + str(people[employee]['ФИО'])
        lines, rows = [], []
        for task in subset:
            key = str(task['Номер задачи']).strip()
            pending = self.pending_report(key)
            lines.append(f"№{key} · {task['Задача'][:220]}\n{task['ФИО']}\nСрок: {task['Дедлайн'] or 'не указан'} · {task['Статус задачи']}" + (' · На проверке' if pending else ''))
            rows.append([button(f"№{key} · {task['Задача'][:45]}", 'card:' + token(key))])
        prefix = 'staff:' + token(employee) if category == 'staff' else 'list:' + category
        rows += self.navigation(len(selected), page, prefix)
        rows.append([button('← К сотрудникам' if category == 'staff' else '← Меню', 'people:0' if category == 'staff' else 'home')])
        self.send(uid, f'{title}: {len(selected)}. Страница {page+1}\n\n' + ('\n\n'.join(lines) if lines else 'Задач нет.'), keyboard(rows))

    def overview(self, uid):
        self.admin(uid)
        tasks, _ = self.sheets.snapshot()
        active = [t for _, t in tasks if t['Статус задачи'] not in CLOSED]
        now = datetime.now(MOSCOW)
        overdue = sum(bool(deadline(t['Дедлайн']) and deadline(t['Дедлайн']) < now) for t in active)
        review = sum(bool(self.pending_report(str(t['Номер задачи']).strip())) for t in active)
        text = f'Обзор задач\nОткрытых: {len(active)}\nПросрочено: {overdue}\nБез срока: {sum(not str(t["Дедлайн"]).strip() for t in active)}\nНа проверке: {review}'
        self.send(uid, text, keyboard([[button('📋 Открытые', 'list:open:0'), button('⏰ Просрочены', 'list:overdue:0')],
            [button('📅 Без срока', 'list:nodue:0'), button('📥 На проверке', 'list:review:0')],
            [button('📅 Запросы переноса', 'list:requests:0')],
            [button(PEOPLE, 'people:0')], [button('⚙️ Напоминания', 'reminders')]]))

    def card(self, uid, digest):
        tasks, people = self.sheets.snapshot()
        _, task = self.resolve(tasks, digest)
        if uid not in self.admins:
            self.owned(uid, task, people, open_only=False)
        key = str(task['Номер задачи']).strip()
        pending = self.pending_report(key)
        accepted = self.db.execute('SELECT revision FROM task_acceptances WHERE task=?', (key,)).fetchone()
        state = task['Статус задачи']
        text = f"Задача №{key}\n\n{task['Задача']}\n\nИсполнитель: {task['ФИО']}\nСрок: {task['Дедлайн'] or 'не указан'} (МСК)\nСтатус: {state}\nПринята сотрудником: {'Да' if accepted and accepted[0] == revision(task) else 'Нет'}"
        rows = []
        if state not in CLOSED:
            if uid in self.admins:
                rows.append([button('✏️ Изменить поручение', 'edit:' + digest)])
            if self.store.chat(str(task['ID сотрудника']).strip()) == uid:
                if not accepted or accepted[0] != revision(task):
                    rows.append([button('✅ Принять задачу', 'accept:' + digest)])
                rows += [[button('✅ Сообщить о завершении', 'finish:' + digest)], [button('📅 Запросить перенос', 'move:' + digest)]]
        if pending:
            text += '\nОтчёт ожидает проверки администратора.'
            if uid in self.admins and pending['state'] == 'pending':
                rows.append([button('📎 Открыть отчёт и результат', 'report:' + pending['id'])])
        requests = self.db.execute("SELECT id FROM deadline_requests WHERE task=? AND state='pending'", (key,)).fetchall()
        if uid in self.admins:
            for request in requests:
                rows.append([button('📅 Рассмотреть перенос', 'request:' + request[0])])
        rows.append([button('← К задачам', 'list:open:0' if uid in self.admins else 'list:mine:0')])
        self.send(uid, text, keyboard(rows))

    def begin_report(self, uid, key):
        tasks, people = self.sheets.snapshot()
        _, task = find_task(tasks, key)
        self.owned(uid, task, people)
        if self.pending_report(key):
            self.send(uid, 'Отчёт уже ожидает решения администратора.')
            return
        data = self.dialog(uid, {'step': 'evidence', 'task': key, 'original': task, 'evidence': []})
        self.send(uid, f'Завершение задачи №{key}.\nПришлите результат: документ, фото, ссылку или комментарий. Можно добавить несколько сообщений, затем нажать «Отправить отчёт». Вложения необязательны.\n/cancel — отмена.', keyboard([[button('📨 Отправить отчёт', 'submit:' + data['nonce'])]]))

    def accept(self, uid, digest):
        tasks, people = self.sheets.snapshot()
        row, task = self.resolve(tasks, digest)
        self.owned(uid, task, people)
        key = str(task['Номер задачи']).strip()
        if self.pending_report(key):
            raise ValueError('Задача уже на проверке.')
        accepted = self.db.execute('SELECT revision FROM task_acceptances WHERE task=?', (key,)).fetchone()
        if accepted and accepted[0] == revision(task):
            return 'Вы уже приняли эту задачу.'
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO task_acceptances VALUES(?,?,?)', (key, str(task['ID сотрудника']).strip(), revision(task)))
        for admin in self.admins:
            self.queue(f'accepted:{key}:{revision(task)}:{admin}', admin, f"✅ {task['ФИО']} принял(а) задачу №{key}: {task['Задача']}", guard={'admin': True})
        self.flush()
        return 'Задача принята. Руководитель уведомлён.'

    def begin_move(self, uid, key):
        tasks, people = self.sheets.snapshot()
        _, task = find_task(tasks, key)
        self.owned(uid, task, people)
        if self.pending_report(key):
            raise ValueError('Задача на проверке. Дождитесь решения администратора.')
        if self.db.execute("SELECT 1 FROM deadline_requests WHERE task=? AND state IN ('pending','approving')", (key,)).fetchone():
            raise ValueError('Запрос переноса по этой задаче уже ожидает решения.')
        self.dialog(uid, {'step': 'move_date', 'task': key, 'original': task})
        self.send(uid, 'Введите предлагаемый срок: ДД.ММ.ГГГГ ЧЧ:ММ, по Москве.\n/cancel — отмена.')

    def return_prompt(self, uid, report_id):
        self.admin(uid)
        record = self.db.execute('SELECT * FROM completions WHERE id=?', (report_id,)).fetchone()
        if not record or record['state'] != 'pending':
            raise ValueError('Отчёт уже рассмотрен или не найден.')
        self.dialog(uid, {'step': 'return_reason', 'report': report_id})
        self.send(uid, 'Что нужно исправить? Напишите причину возврата (до 2000 символов).\n/cancel — отмена.')

    def show_report(self, uid, report_id):
        self.admin(uid)
        record = self.db.execute('SELECT * FROM completions WHERE id=?', (report_id,)).fetchone()
        if not record or record['state'] != 'pending':
            raise ValueError('Отчёт уже рассмотрен или не найден.')
        # A deliberate user request reopens the report, without changing delivery history.
        copy = dict(record)
        copy['notices'] = '{}'
        CompletionFlow(self.store, self.tg, self.sheets, {uid}).notify_admins(copy, remember=False)

    def request_card(self, uid, request_id):
        self.admin(uid)
        request = self.db.execute('SELECT * FROM deadline_requests WHERE id=?', (request_id,)).fetchone()
        if not request or request['state'] != 'pending':
            raise ValueError('Запрос уже рассмотрен или не найден.')
        original = json.loads(request['original'])
        self.send(uid, f"Запрос переноса задачи №{request['task']}\n{original['ФИО']}\n{original['Задача']}\n\nБыло: {original['Дедлайн'] or 'без срока'}\nПредлагается: {request['proposed']} (МСК)\nПричина: {request['reason']}", keyboard([[button('✅ Подтвердить перенос', 'move_yes:' + request_id)], [button('❌ Отклонить', 'move_no:' + request_id)]]))

    def mutate(self, uid, task, changes, source='', mutation_id=None):
        mutation_id = mutation_id or secrets.token_hex(12)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO task_mutations(id,task,expected,changes,source,author) VALUES(?,?,?,?,?,?)',
                (mutation_id, str(task['Номер задачи']).strip(), json.dumps(task, ensure_ascii=False), json.dumps(changes, ensure_ascii=False), source, uid))
        return mutation_id

    def apply_mutations(self):
        for record in self.db.execute("SELECT * FROM task_mutations WHERE state='pending'").fetchall():
            try:
                tasks, people = self.sheets.snapshot()
                row, current = find_task(tasks, record['task'])
                expected, changes = json.loads(record['expected']), json.loads(record['changes'])
                expected_employee = str(expected['ID сотрудника']).strip()
                if record['source']:
                    request = self.db.execute('SELECT * FROM deadline_requests WHERE id=?', (record['source'],)).fetchone()
                    if not request or self.store.chat(expected_employee) != request['chat']:
                        raise ValueError('Привязка сотрудника изменилась')
                destination = str(changes.get('ID сотрудника', expected_employee)).strip()
                person = people.get(destination)
                if not person or not yes(person['Активен']) or str(person['ФИО']).strip() != str(changes.get('ФИО', expected['ФИО'])).strip():
                    raise ValueError('Исполнитель неактивен или ФИО изменено')
                if current['Статус задачи'] in CLOSED:
                    raise ValueError('Задача выполнена или отменена')
                if self.pending_report(record['task']):
                    raise ValueError('Сначала рассмотрите отчёт о завершении')
                # Recover a successful write whose response was lost: compare its intended values.
                desired = dict(expected)
                for column, value in changes.items():
                    desired[column] = (datetime(1899, 12, 30) + timedelta(days=value)).strftime('%d.%m.%Y %H:%M') if column == 'Дедлайн' and isinstance(value, (int, float)) else value
                def matches(left, right):
                    return all((deadline(left[k]) == deadline(right[k]) if k == 'Дедлайн' else str(left[k]).strip() == str(right[k]).strip()) for k in TASK_HEADERS[:8])
                if not matches(current, desired):
                    if not matches(current, expected):
                        raise ValueError('Задача изменилась после открытия карточки')
                    self.sheets.modify(row, current, changes)
                with self.db:
                    self.db.execute("UPDATE task_mutations SET state='applied' WHERE id=?", (record['id'],))
                    self.db.execute('DELETE FROM task_acceptances WHERE task=?', (record['task'],))
                    self.db.execute("UPDATE completions SET state='stale' WHERE task=? AND state='pending'", (record['task'],))
                    self.db.execute("UPDATE deadline_requests SET state='stale' WHERE task=? AND state='pending' AND id!=?", (record['task'], record['source']))
                    if record['source']:
                        self.db.execute("UPDATE deadline_requests SET state='approved' WHERE id=?", (record['source'],))
                    self.queue('mutation:' + record['id'], record['author'], f"Изменения задачи №{record['task']} записаны в таблицу.", guard={'admin': True})
            except ValueError as exc:
                with self.db:
                    self.db.execute("UPDATE task_mutations SET state='stale' WHERE id=?", (record['id'],))
                    if record['source']:
                        self.db.execute("UPDATE deadline_requests SET state='stale' WHERE id=?", (record['source'],))
                    self.queue('mutation-error:' + record['id'], record['author'], f"Изменения задачи №{record['task']} не применены: {exc}. Откройте карточку заново.", guard={'admin': True})
            except RuntimeError:
                LOG.warning('Изменения задачи ожидают синхронизации')

    def edit(self, uid, digest):
        self.admin(uid)
        tasks, _ = self.sheets.snapshot()
        _, task = self.resolve(tasks, digest)
        if task['Статус задачи'] in CLOSED:
            raise ValueError('Задача выполнена или отменена.')
        if self.pending_report(str(task['Номер задачи']).strip()):
            raise ValueError('Сначала рассмотрите отчёт о завершении задачи.')
        data = self.dialog(uid, {'step': 'edit_pick', 'task': str(task['Номер задачи']).strip(), 'original': task})
        self.send(uid, f"Что изменить в задаче №{data['task']}?", keyboard([[button('Содержание', 'field:text:' + data['nonce'])], [button('Дедлайн', 'field:due:' + data['nonce'])], [button('Исполнитель', 'field:employee:' + data['nonce'])]]))

    def preview(self, uid, data, changes):
        data['step'], data['changes'] = 'edit_confirm', changes
        self.dialog(uid, data)
        lines = []
        for key, value in changes.items():
            if key == 'ID сотрудника':
                continue
            display = (datetime(1899, 12, 30) + timedelta(days=value)).strftime('%d.%m.%Y %H:%M') if key == 'Дедлайн' and isinstance(value, (int, float)) else value
            lines.append(f'{key}: {display or "без срока"}')
        self.send(uid, f"Изменения задачи №{data['task']}:\n" + '\n'.join(lines) + '\n\nЗаписать и уведомить сотрудника?', keyboard([[button('✅ Сохранить изменения', 'save:' + data['nonce'])], [button('Отмена', 'cancel')]]))

    def settings(self, uid):
        self.admin(uid)
        enabled = self.db.execute("SELECT value FROM settings WHERE key='reminders_enabled'").fetchone()
        hours = self.reminder_hours()
        mode = enabled is None or enabled[0] == '1'
        self.send(uid, f"Напоминания: {'включены' if mode else 'выключены'}\nДо дедлайна: {', '.join(map(str, hours))} ч.\nСотруднику: одно сообщение при просрочке. Администратору: список просроченных раз в день после 09:00 МСК.\nЗадачи на проверке не тревожат сотрудника.", keyboard([[button('Выключить' if mode else 'Включить', 'remind_toggle')], [button('Изменить интервалы', 'remind_hours')]]))

    def reminder_hours(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='reminder_hours'").fetchone()
        return json.loads(row[0]) if row else [24, 1]

    def observe(self, tasks, people):
        # Seed once per task; deploying this feature never resends existing assignments.
        counts = {}
        for _, task in tasks:
            key = str(task['Номер задачи']).strip()
            counts[key] = counts.get(key, 0) + 1
        for _, task in tasks:
            key = str(task['Номер задачи']).strip()
            if not key or counts[key] != 1:
                continue
            observed = self.db.execute('SELECT payload FROM observed_tasks WHERE task=?', (key,)).fetchone()
            employee = str(task['ID сотрудника']).strip()
            person = people.get(employee)
            if not observed:
                with self.db:
                    self.db.execute('INSERT INTO observed_tasks VALUES(?,?)', (key, json.dumps(task, ensure_ascii=False)))
                continue
            previous = json.loads(observed[0])
            if revision(previous) != revision(task):
                if (not person or not yes(person['Активен']) or str(person['ФИО']).strip() != str(task['ФИО']).strip()
                        or task['Статус задачи'] in CLOSED or not yes(task['Готова к отправке'])):
                    continue
                chat = self.store.chat(employee)
                if chat is None:
                    continue  # Keep the old observation until the new assignee connects.
                delivery = self.store.get(key)
                if (delivery and delivery['state'] == 'sent') or task['Статус уведомления'] == 'Отправлено':
                    mark = secrets.token_hex(12)
                    with self.db:
                        self.queue(f'changed:{key}:{mark}:{chat}', chat, f"✏️ Поручение №{key} изменено.\n\n{task['Задача']}\nИсполнитель: {task['ФИО']}\nСрок: {task['Дедлайн'] or 'не указан'} (МСК)", keyboard([[button('Открыть задачу', 'card:' + token(key))]]), {'employee': employee})
                        old_employee = str(previous['ID сотрудника']).strip()
                        old_chat = self.store.chat(old_employee)
                        if old_employee != employee and old_chat is not None:
                            self.queue(f'reassigned:{key}:{mark}:{old_chat}', old_chat, f'Задача №{key} передана другому сотруднику и больше не назначена вам.', guard={'employee': old_employee})
                with self.db:
                    self.db.execute('DELETE FROM task_acceptances WHERE task=?', (key,))
                    self.db.execute("UPDATE completions SET state='stale' WHERE task=? AND state='pending'", (key,))
                    self.db.execute("UPDATE deadline_requests SET state='stale' WHERE task=? AND state='pending'", (key,))
                    self.db.execute('UPDATE observed_tasks SET payload=? WHERE task=?', (json.dumps(task, ensure_ascii=False), key))

    def reminders(self, tasks, people, now=None):
        enabled = self.db.execute("SELECT value FROM settings WHERE key='reminders_enabled'").fetchone()
        if enabled and enabled[0] == '0':
            return
        now = now or datetime.now(MOSCOW)
        overdue = []
        counts = {}
        for _, task in tasks:
            key = str(task['Номер задачи']).strip()
            counts[key] = counts.get(key, 0) + 1
        for _, task in tasks:
            key = str(task['Номер задачи']).strip()
            due = deadline(task['Дедлайн'])
            if not key or counts[key] != 1 or task['Статус задачи'] in CLOSED or not due or not yes(task['Готова к отправке']):
                continue
            employee = str(task['ID сотрудника']).strip()
            person = people.get(employee)
            if not person or not yes(person['Активен']) or str(person['ФИО']).strip() != str(task['ФИО']).strip():
                continue
            if due < now:
                overdue.append(task)
            if self.pending_report(key):
                continue
            chat = self.store.chat(employee)
            if chat is None:
                continue
            remaining = (due - now).total_seconds() / 3600
            bands = sorted(self.reminder_hours())
            # Select only the nearest crossed threshold after an outage, not every reminder.
            band = next((h for h in bands if 0 < remaining <= h), None)
            if remaining <= 0:
                label, category = 'Срок задачи прошёл', 'late'
            elif band is not None:
                label, category = f'До срока задачи осталось менее {band} ч.', f'before:{band}'
            else:
                continue
            self.queue(f'reminder:{key}:{revision(task)}:{category}:{chat}', chat, f"⏰ {label}\nЗадача №{key}: {task['Задача']}\nДедлайн: {task['Дедлайн']} (МСК)", keyboard([[button('Открыть задачу', 'card:' + token(key))]]), {'employee': employee, 'task': key, 'revision': revision(task), 'reminder': True})
        if overdue and now.hour >= 9:
            lines = [f"№{t['Номер задачи']} · {t['ФИО']} · {t['Дедлайн']}\n{t['Задача'][:200]}" for t in overdue]
            for admin in self.admins:
                self.queue(f'overdue:{now.date()}:{admin}', admin, f'⏰ Просроченные задачи: {len(overdue)}\n\n' + '\n\n'.join(lines), keyboard([[button('Открыть просроченные', 'list:overdue:0')]]), {'admin': True, 'reminder': True})

    def cycle(self):
        tasks, people = self.sheets.snapshot()
        self.observe(tasks, people)
        self.apply_mutations()
        tasks, people = self.sheets.snapshot()
        self.observe(tasks, people)
        self.reminders(tasks, people)
        self.flush()

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if callback else update.get('message', {})
        uid = (callback or message).get('from', {}).get('id')
        chat = message.get('chat', {})
        if chat.get('type') != 'private' or chat.get('id') != uid:
            if callback:
                self.tg.call('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': 'Откройте личный чат с ботом.'})
            return bool(callback)
        try:
            if callback:
                handled = self.callback(uid, callback.get('data', ''), message)
                if handled:
                    self.tg.call('answerCallbackQuery', {'callback_query_id': callback['id']})
                return handled
            text = message.get('text', '').strip()
            routes = {MY: 'mine', '/tasks': 'mine', OVERVIEW: 'overview', '/overview': 'overview', REVIEW: 'review', '/review': 'review', PEOPLE: 'people', '/employees': 'people'}
            command = text.split('@')[0] if text.startswith('/') and ' ' not in text else text
            if command in routes:
                self.clear(uid)
                route = routes[command]
                if route == 'overview':
                    self.overview(uid)
                elif route == 'people':
                    self.employees(uid)
                else:
                    self.task_list(uid, route)
                return True
            if text in (FINISH_BUTTON, MOVE):
                binding = self.db.execute('SELECT employee FROM bindings WHERE chat=?', (uid,)).fetchone()
                if not binding:
                    raise ValueError('Сначала подключитесь как сотрудник по приглашению.')
                self.clear(uid)
                self.dialog(uid, {'step': 'report_number' if text == FINISH_BUTTON else 'move_number'})
                self.send(uid, 'Введите номер задачи.\n/cancel — отмена.')
                return True
            if text.startswith('/'):
                self.clear(uid)
                if text == '/cancel':
                    self.send(uid, 'Действие отменено.', menu(uid in self.admins))
                    return True
                return False
            data = self.dialog(uid)
            if data:
                self.input(uid, data, message)
                return True
            if message.get('document') or message.get('photo'):
                self.send(uid, 'Сначала выберите задачу и нажмите «Сообщить о завершении», затем прикрепите результат.')
                return True
            return False
        except ValueError as exc:
            self.send(uid, str(exc))
        except RuntimeError:
            self.send(uid, 'Таблица сейчас недоступна. Повторите действие позже; введённые данные сохранены.')
        if callback:
            self.tg.call('answerCallbackQuery', {'callback_query_id': callback['id']})
        return True

    def callback(self, uid, data, message=None):
        fields = data.split(':')
        action = fields[0]
        if action == 'approve':
            return False  # Existing completion handler applies the durable decision.
        if action == 'return':
            self.return_prompt(uid, fields[1])
        elif action == 'report':
            self.show_report(uid, fields[1])
        elif action == 'home':
            self.clear(uid)
            self.send(uid, 'Выберите действие.', menu(uid in self.admins))
        elif action == 'people':
            self.clear(uid)
            self.employees(uid, fields[1])
        elif action == 'staff':
            self.admin(uid)
            _, people = self.sheets.snapshot()
            self.task_list(uid, 'staff', self.person(people, fields[1]), fields[2])
        elif action == 'list':
            self.clear(uid)
            if fields[1] not in ('mine', 'open', 'review', 'requests', 'overdue', 'nodue'):
                raise ValueError('Неизвестный список задач.')
            self.task_list(uid, fields[1], page=fields[2])
        elif action == 'card':
            self.clear(uid)
            self.card(uid, fields[1])
        elif action in ('finish', 'move'):
            tasks, _ = self.sheets.snapshot()
            _, task = self.resolve(tasks, fields[1])
            key = str(task['Номер задачи']).strip()
            self.clear(uid)
            self.begin_report(uid, key) if action == 'finish' else self.begin_move(uid, key)
        elif action == 'accept':
            reply = self.accept(uid, fields[1])
            if message and message.get('message_id'):
                original = message.get('reply_markup', {}).get('inline_keyboard', [])
                rows = [[b for b in row if b.get('callback_data') != data] for row in original]
                rows = [row for row in rows if row]
                if rows != original:
                    try:
                        self.tg.call('editMessageReplyMarkup', {
                            'chat_id': uid, 'message_id': message['message_id'],
                            'reply_markup': keyboard(rows),
                        })
                    except APIError:
                        LOG.warning('Не удалось скрыть кнопку принятия; задача принята, повторное нажатие обновит кнопки')
            self.send(uid, reply)
        elif action == 'cancel':
            self.clear(uid)
            self.send(uid, 'Действие отменено.', menu(uid in self.admins))
        elif action in ('submit', 'save', 'field', 'assignee'):
            dialog = self.dialog(uid)
            nonce = fields[2] if action in ('field', 'assignee') else fields[1]
            if not dialog or dialog['nonce'] != nonce:
                raise ValueError('Кнопка устарела. Начните действие заново.')
            if action == 'submit':
                if dialog['step'] != 'evidence':
                    raise ValueError('Отчёт не готов к отправке.')
                tasks, people = self.sheets.snapshot()
                _, current = find_task(tasks, dialog['task'])
                self.owned(uid, current, people)
                if revision(current) != revision(dialog['original']):
                    raise ValueError('Задача изменилась. Откройте её заново.')
                reply = CompletionFlow(self.store, self.tg, self.sheets, self.admins).report(uid, dialog['task'], dialog['evidence'])
                self.clear(uid)
                self.send(uid, reply, menu(uid in self.admins))
            else:
                self.admin(uid)
                if action == 'field':
                    if dialog['step'] != 'edit_pick':
                        raise ValueError('Кнопка устарела.')
                    field = fields[1]
                    if field == 'employee':
                        dialog['step'] = 'edit_employee'
                        self.dialog(uid, dialog)
                        self.assignees(uid, dialog, 0)
                    elif field in ('text', 'due'):
                        dialog['step'] = 'edit_' + field
                        self.dialog(uid, dialog)
                        self.send(uid, 'Напишите новое содержание (до 8000 символов).' if field == 'text' else 'Введите срок ДД.ММ.ГГГГ ЧЧ:ММ по Москве или «Без срока».')
                    else:
                        raise ValueError('Неизвестное поле.')
                elif action == 'assignee':
                    if dialog['step'] != 'edit_employee':
                        raise ValueError('Кнопка устарела.')
                    _, people = self.sheets.snapshot()
                    employee = self.person(people, fields[1])
                    if not yes(people[employee]['Активен']):
                        raise ValueError('Сотрудник неактивен.')
                    self.preview(uid, dialog, {'ID сотрудника': employee, 'ФИО': people[employee]['ФИО']})
                elif action == 'save':
                    if dialog['step'] != 'edit_confirm':
                        raise ValueError('Кнопка устарела.')
                    self.mutate(uid, dialog['original'], dialog['changes'], mutation_id=dialog['nonce'])
                    self.clear(uid)
                    self.cycle()
                    self.send(uid, 'Изменение сохранено для синхронизации. Результат придёт отдельным сообщением.')
        elif action == 'assign_page':
            self.admin(uid)
            dialog = self.dialog(uid)
            if not dialog or dialog['nonce'] != fields[1] or dialog['step'] != 'edit_employee':
                raise ValueError('Выбор сотрудника устарел.')
            self.assignees(uid, dialog, fields[2])
        elif action == 'edit':
            self.clear(uid)
            self.edit(uid, fields[1])
        elif action == 'request':
            self.request_card(uid, fields[1])
        elif action in ('move_yes', 'move_no'):
            self.admin(uid)
            request = self.db.execute('SELECT * FROM deadline_requests WHERE id=?', (fields[1],)).fetchone()
            if not request or request['state'] != 'pending':
                raise ValueError('Решение по запросу уже принято.')
            tasks, people = self.sheets.snapshot()
            _, task = find_task(tasks, request['task'])
            original = json.loads(request['original'])
            if revision(task) != revision(original) or task['Статус задачи'] in CLOSED or self.store.chat(request['employee']) != request['chat']:
                with self.db:
                    self.db.execute("UPDATE deadline_requests SET state='stale' WHERE id=?", (request['id'],))
                raise ValueError('Запрос устарел: задача или привязка изменена.')
            if action == 'move_yes':
                with self.db:
                    self.mutate(uid, task, {'Дедлайн': serial(input_deadline(request['proposed']))}, request['id'], request['id'])
                    self.db.execute("UPDATE deadline_requests SET state='approving' WHERE id=?", (request['id'],))
                self.cycle()
                self.send(uid, 'Подтверждение переноса сохранено. Результат записи придёт отдельно.')
            else:
                with self.db:
                    self.db.execute("UPDATE deadline_requests SET state='rejected' WHERE id=?", (request['id'],))
                    self.queue('move-rejected:' + request['id'], request['chat'], f"Перенос срока задачи №{request['task']} отклонён. Действует прежний срок: {original['Дедлайн'] or 'не указан'}.", guard={'employee': request['employee']})
                self.flush()
                self.send(uid, 'Запрос переноса отклонён.')
        elif action in ('reminders', 'remind_toggle', 'remind_hours'):
            self.admin(uid)
            if action == 'remind_toggle':
                old = self.db.execute("SELECT value FROM settings WHERE key='reminders_enabled'").fetchone()
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO settings VALUES('reminders_enabled',?)", ('1' if old and old[0] == '0' else '0',))
            if action == 'remind_hours':
                self.dialog(uid, {'step': 'reminder_hours'})
                self.send(uid, 'Введите интервалы в часах через запятую, например: 24, 1. От 1 до 168 часов, максимум 5 интервалов.')
            else:
                self.settings(uid)
        else:
            return False
        return True

    def assignees(self, uid, data, page):
        _, people = self.sheets.snapshot()
        active = sorted(((key, p) for key, p in people.items() if yes(p['Активен'])), key=lambda item: str(item[1]['ФИО']).casefold())
        page, subset = self.page(active, page)
        rows = [[button(str(person['ФИО']), 'assignee:' + token(key) + ':' + data['nonce'])] for key, person in subset]
        rows += self.navigation(len(active), page, 'assign_page:' + data['nonce'])
        self.send(uid, f'Выберите исполнителя. Страница {page+1}', keyboard(rows))

    def input(self, uid, data, message):
        text = message.get('text', '').strip()
        step = data['step']
        if step in ('report_number', 'move_number'):
            if not text:
                raise ValueError('Введите номер задачи текстом.')
            self.begin_report(uid, text) if step == 'report_number' else self.begin_move(uid, text)
        elif step == 'evidence':
            if len(data['evidence']) >= 10:
                raise ValueError('Можно добавить до 10 вложений или сообщений. Нажмите «Отправить отчёт».')
            item = None
            if message.get('document'):
                item = {'kind': 'document', 'file_id': message['document']['file_id'], 'caption': message.get('caption', '')[:800]}
            elif message.get('photo'):
                item = {'kind': 'photo', 'file_id': message['photo'][-1]['file_id'], 'caption': message.get('caption', '')[:800]}
            elif text and len(text.encode('utf-16-le')) // 2 <= 3000:
                item = {'kind': 'text', 'text': text}
            if not item:
                raise ValueError('Пришлите документ, фото, ссылку или комментарий до 3000 символов.')
            data['evidence'].append(item)
            self.dialog(uid, data)
            self.send(uid, f"Результат добавлен. Сообщений/вложений: {len(data['evidence'])}.", keyboard([[button('📨 Отправить отчёт', 'submit:' + data['nonce'])]]))
        elif step == 'return_reason':
            self.admin(uid)
            if not text or len(text.encode('utf-16-le')) // 2 > 2000:
                raise ValueError('Напишите причину возврата от 1 до 2000 символов.')
            reply = CompletionFlow(self.store, self.tg, self.sheets, self.admins).decide(uid, data['report'], 'return', text)
            self.clear(uid)
            self.send(uid, reply)
        elif step == 'move_date':
            proposed = input_deadline(text)
            if proposed <= datetime.now(MOSCOW):
                raise ValueError('Новый срок должен быть в будущем.')
            data['proposed'], data['step'] = proposed.strftime('%d.%m.%Y %H:%M'), 'move_reason'
            self.dialog(uid, data)
            self.send(uid, 'Напишите причину переноса (до 2000 символов).')
        elif step == 'move_reason':
            if not text or len(text) > 2000:
                raise ValueError('Напишите причину от 1 до 2000 символов.')
            tasks, people = self.sheets.snapshot()
            _, task = find_task(tasks, data['task'])
            self.owned(uid, task, people)
            if revision(task) != revision(data['original']) or self.pending_report(data['task']):
                raise ValueError('Задача изменилась или отправлена на проверку. Откройте её заново.')
            if not self.admins:
                raise ValueError('Администратор не настроен.')
            request_id = data['nonce']
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO deadline_requests(id,task,employee,chat,original,proposed,reason) VALUES(?,?,?,?,?,?,?)',
                    (request_id, data['task'], str(task['ID сотрудника']).strip(), uid, json.dumps(task, ensure_ascii=False), data['proposed'], text))
                for admin in self.admins:
                    self.queue(f'move-request:{request_id}:{admin}', admin, f"📅 {task['ФИО']} просит перенести срок задачи №{data['task']}.\n{task['Задача']}\nБыло: {task['Дедлайн'] or 'без срока'}\nНовый срок: {data['proposed']} (МСК)\nПричина: {text}", keyboard([[button('Рассмотреть запрос', 'request:' + request_id)]]), {'admin': True})
            self.clear(uid)
            self.flush()
            self.send(uid, 'Запрос переноса передан администратору. До подтверждения действует прежний срок.')
        elif step in ('edit_text', 'edit_due'):
            self.admin(uid)
            if step == 'edit_text':
                if not text or len(text) > 8000:
                    raise ValueError('Содержание должно содержать от 1 до 8000 символов.')
                changes = {'Задача': text}
            else:
                changes = {'Дедлайн': '' if text.casefold() == 'без срока' else serial(input_deadline(text))}
            self.preview(uid, data, changes)
        elif step == 'reminder_hours':
            self.admin(uid)
            try:
                hours = sorted(set(int(x.strip()) for x in text.split(',')), reverse=True)
            except ValueError:
                raise ValueError('Введите целые часы через запятую: 24, 1.') from None
            if not 1 <= len(hours) <= 5 or any(not 1 <= h <= 168 for h in hours):
                raise ValueError('Нужно от 1 до 5 интервалов, каждый от 1 до 168 часов.')
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES('reminder_hours',?)", (json.dumps(hours),))
            self.clear(uid)
            self.settings(uid)
        else:
            raise ValueError('Выберите действие кнопкой или отправьте /cancel.')
