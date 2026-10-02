"""Personal task notifications. Python 3.11+. One running instance per database."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __name__ == '__main__':
    # The Windows direct entrypoint and imported workflows share exception classes.
    sys.modules['bot'] = sys.modules[__name__]

LOG = logging.getLogger('taskbot')
MOSCOW = timezone(timedelta(hours=3))
TASK_HEADERS = ['Номер задачи', 'Дата создания', 'Задача', 'ФИО', 'Дедлайн',
                'Статус задачи', 'ID сотрудника', 'Готова к отправке',
                'Статус уведомления', 'Время отправки', 'Ошибка уведомления']
PEOPLE_HEADERS = ['ID сотрудника', 'ФИО', 'Варианты обращения', 'Подразделение', 'Активен']


def load_env(path='.env'):
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            key, sep, value = line.partition('=')
            if sep:
                os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))


class APIError(Exception):
    def __init__(self, kind, message, retry_after=60):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


class Telegram:
    def __init__(self, token):
        self.base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, payload=None):
        req = urllib.request.Request(self.base + method,
                                     data=json.dumps(payload or {}).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read())
            except (ValueError, OSError):
                data = {'error_code': exc.code}
        except (OSError, ValueError):
            # A timeout may happen after Telegram has accepted sendMessage.
            raise APIError('uncertain', 'Нет однозначного ответа Telegram') from None
        if data.get('ok'):
            return data['result']
        code = data.get('error_code', 500)
        if code == 429:
            raise APIError('retry', 'Ограничение частоты Telegram',
                           data.get('parameters', {}).get('retry_after', 60))
        if code in (401, 409):
            raise APIError('fatal', 'Проверьте токен и отсутствие другого процесса/webhook')
        if code in (400, 403):
            raise APIError('permanent', 'Telegram отклонил сообщение: чат недоступен или запрос некорректен')
        raise APIError('uncertain', 'Неопределённый результат Telegram')

    def send(self, chat_id, text, reply_markup=None):
        payload = {'chat_id': chat_id, 'text': text}
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        return self.call('sendMessage', payload)['message_id']


def col(index):
    result = ''
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


class Sheets:
    def __init__(self, credentials_path, spreadsheet_id, task_sheet, people_sheet, max_rows):
        from google.oauth2.service_account import Credentials
        from google.auth.transport.requests import AuthorizedSession
        creds = Credentials.from_service_account_file(
            credentials_path, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        self.session = AuthorizedSession(creds)
        self.base = 'https://sheets.googleapis.com/v4/spreadsheets/' + spreadsheet_id
        self.task_sheet, self.people_sheet, self.max_rows = task_sheet, people_sheet, max_rows

    def request(self, method, suffix='', **kwargs):
        try:
            response = self.session.request(method, self.base + suffix, timeout=30, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception:
            # Do not log credential paths, HTTP URLs or raw API responses.
            raise RuntimeError('Ошибка Google Sheets: проверьте сеть, доступ и настройки') from None

    @staticmethod
    def address(sheet, cells):
        return "'" + sheet.replace("'", "''") + "'!" + cells

    def metadata(self):
        return self.request('GET', params={'fields': 'properties(timeZone),sheets(properties(title,gridProperties(rowCount)))'})

    def read(self, sheet, last_col, headers):
        end_row = getattr(self, 'row_counts', {}).get(sheet, self.max_rows)
        address = self.address(sheet, f'A1:{last_col}{end_row}')
        data = self.request('GET', '/values/' + urllib.parse.quote(address, safe=''),
                            params={'valueRenderOption': 'FORMATTED_VALUE'}).get('values', [])
        if not data or data[0] != headers:
            raise RuntimeError(f'Не совпадают заголовки листа {sheet}')
        return [(i, dict(zip(headers, row + [''] * (len(headers) - len(row)))))
                for i, row in enumerate(data[1:], 2) if any(str(v).strip() for v in row)]

    def snapshot(self):
        info = self.metadata()
        titles = {s['properties']['title']: s['properties']['gridProperties']['rowCount'] for s in info['sheets']}
        if any(s not in titles or titles[s] > self.max_rows for s in (self.task_sheet, self.people_sheet)):
            raise RuntimeError('Проверьте названия листов и MAX_ROWS')
        self.row_counts = titles
        if info.get('properties', {}).get('timeZone') != 'Europe/Moscow':
            raise RuntimeError('Часовой пояс таблицы должен быть Europe/Moscow')
        tasks = self.read(self.task_sheet, 'K', TASK_HEADERS)
        people_rows = self.read(self.people_sheet, 'E', PEOPLE_HEADERS)
        ids = [str(p['ID сотрудника']).strip() for _, p in people_rows]
        if any(not key for key in ids) or len(ids) != len(set(ids)):
            raise RuntimeError('В справочнике есть пустые или повторные ID сотрудников')
        return tasks, {str(p['ID сотрудника']).strip(): p for _, p in people_rows}

    def mirror(self, row, task_id, status, sent_at, error):
        # Verify the key immediately before a row-based Sheets write.
        cell = self.address(self.task_sheet, f'A{row}')
        found = self.request('GET', '/values/' + urllib.parse.quote(cell, safe='')).get('values', [])
        if not found or str(found[0][0]).strip() != task_id:
            raise RuntimeError('Строка перемещена; повторная синхронизация в следующем цикле')
        timestamp = ''
        if sent_at:
            date = datetime.fromisoformat(sent_at).astimezone(MOSCOW).replace(tzinfo=None)
            timestamp = (date - datetime(1899, 12, 30)).total_seconds() / 86400
        self.request('POST', '/values:batchUpdate', json={'valueInputOption': 'RAW', 'data': [
            {'range': self.address(self.task_sheet, f'I{row}:K{row}'),
             'values': [[status, timestamp, error]]}]})

    def complete(self, row, task_id, payload):
        address = self.address(self.task_sheet, f'A{row}:G{row}')
        values = self.request('GET', '/values/' + urllib.parse.quote(address, safe='')).get('values', [])
        found = dict(zip(TASK_HEADERS, values[0])) if values else {}
        if not same_task(found, task_id, payload) or found.get('Статус задачи') == 'Отменена':
            raise ValueError('Задача изменена или перемещена. Проверьте таблицу и отправьте новый отчёт.')
        self.request('POST', '/values:batchUpdate', json={'valueInputOption': 'RAW', 'data': [
            {'range': self.address(self.task_sheet, f'F{row}'), 'values': [['Выполнена']]}]})
        result = self.request('GET', '/values/' + urllib.parse.quote(
            self.address(self.task_sheet, f'F{row}'), safe='')).get('values', [])
        if result != [['Выполнена']]:
            raise RuntimeError('Не удалось подтвердить запись статуса в таблицу')

    def modify(self, row, expected, changes):
        # Recheck the full task before writing individual cells. Never rewrite a row.
        address = self.address(self.task_sheet, f'A{row}:H{row}')
        values = self.request('GET', '/values/' + urllib.parse.quote(address, safe=''),
                              params={'valueRenderOption': 'FORMATTED_VALUE'}).get('values', [])
        current = dict(zip(TASK_HEADERS, (values[0] if values else []) + [''] * 8))
        for column in TASK_HEADERS[:8]:
            if str(current.get(column, '')).strip() != str(expected.get(column, '')).strip():
                raise ValueError('Задача изменена или строка перемещена. Откройте карточку заново.')
        allowed = {'Задача', 'ФИО', 'Дедлайн', 'Статус задачи', 'ID сотрудника'}
        if not changes or not changes.keys() <= allowed:
            raise ValueError('Недопустимое изменение задачи')
        data = [{'range': self.address(self.task_sheet, f'{col(TASK_HEADERS.index(k))}{row}'),
                 'values': [[v]]} for k, v in changes.items()]
        self.request('POST', '/values:batchUpdate', json={'valueInputOption': 'RAW', 'data': data})
        found = self.request('GET', '/values/' + urllib.parse.quote(address, safe=''),
                            params={'valueRenderOption': 'UNFORMATTED_VALUE'}).get('values', [])
        raw = dict(zip(TASK_HEADERS, (found[0] if found else []) + [''] * 8))
        if str(raw.get('Номер задачи', '')).strip() != str(expected['Номер задачи']).strip():
            raise RuntimeError('Не удалось проверить запись: строка перемещена')
        if any(raw.get(k) != v for k, v in changes.items()):
            raise RuntimeError('Не удалось подтвердить запись изменений')


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS bindings(employee TEXT PRIMARY KEY, chat INTEGER UNIQUE NOT NULL);
          CREATE TABLE IF NOT EXISTS invites(hash TEXT PRIMARY KEY, employee TEXT NOT NULL,
            expires REAL NOT NULL, used_chat INTEGER);
          CREATE TABLE IF NOT EXISTS deliveries(task TEXT PRIMARY KEY, state TEXT NOT NULL,
            payload TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, sent_at TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            mirror TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS completions(id TEXT PRIMARY KEY, task TEXT NOT NULL,
            employee TEXT NOT NULL, chat INTEGER NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', notices TEXT NOT NULL DEFAULT '{}',
            result_progress INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS message_log(task TEXT NOT NULL, part INTEGER NOT NULL,
            chat INTEGER NOT NULL, message_id INTEGER NOT NULL, sent_at TEXT NOT NULL,
            PRIMARY KEY(task,part));
        ''')
        # Crash between durable 'sending' and durable acknowledgement is ambiguous.
        self.db.execute("UPDATE deliveries SET state='uncertain', error='Прервано во время отправки; нужна проверка' WHERE state='sending'")
        self.db.commit()
        # Additive migration preserves bindings, delivery history and old reports.
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(completions)')}
        for name, definition in [('evidence', "TEXT NOT NULL DEFAULT '[]'"),
                                 ('reason', "TEXT NOT NULL DEFAULT ''")]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE completions ADD COLUMN {name} {definition}')
        self.db.commit()

    def invite(self, employee, hours=24):
        token = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute('DELETE FROM invites WHERE employee=? AND used_chat IS NULL', (employee,))
            self.db.execute('INSERT INTO invites VALUES(?,?,?,NULL)',
                            (hashlib.sha256(token.encode()).hexdigest(), employee, time.time() + hours * 3600))
        return token

    def bind(self, token, chat):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db:
            invitation = self.db.execute('SELECT * FROM invites WHERE hash=?', (digest,)).fetchone()
            if not invitation or invitation['expires'] < time.time():
                raise ValueError('Приглашение недействительно или истекло')
            if invitation['used_chat'] is not None:
                if invitation['used_chat'] == chat and self.chat(invitation['employee']) == chat:
                    return invitation['employee']
                raise ValueError('Приглашение уже использовано')
            if self.chat(invitation['employee']) is not None:
                raise ValueError('Сотрудник уже подключён; обратитесь к администратору')
            try:
                self.db.execute('INSERT INTO bindings VALUES(?,?)', (invitation['employee'], chat))
            except sqlite3.IntegrityError:
                raise ValueError('Этот Telegram-аккаунт уже привязан к сотруднику') from None
            self.db.execute('UPDATE invites SET used_chat=? WHERE hash=?', (chat, digest))
            return invitation['employee']

    def chat(self, employee):
        row = self.db.execute('SELECT chat FROM bindings WHERE employee=?', (employee,)).fetchone()
        return row[0] if row else None

    def get(self, task):
        return self.db.execute('SELECT * FROM deliveries WHERE task=?', (task,)).fetchone()

    def set(self, task, **fields):
        allowed = {'state', 'payload', 'progress', 'attempts', 'retry_at', 'sent_at', 'error', 'mirror'}
        if not fields.keys() <= allowed:
            raise ValueError('Invalid database fields')
        with self.db:
            self.db.execute('UPDATE deliveries SET ' + ','.join(k + '=?' for k in fields) + ' WHERE task=?',
                            (*fields.values(), task))

    def enqueue(self, task, payload):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO deliveries(task,state,payload) VALUES(?,'pending',?)",
                            (task, json.dumps(payload, ensure_ascii=False)))

    def offset(self, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES('offset',?)", (str(value),))
        row = self.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def retry(self, task):
        entry = self.get(task)
        if not entry or entry['state'] not in ('permanent', 'uncertain'):
            raise ValueError('Нет ошибки, которую можно повторить')
        self.set(task, state='pending', error='', retry_at=0, mirror='')

    def mark_sent(self, task):
        entry = self.get(task)
        if not entry or entry['state'] != 'uncertain':
            raise ValueError('Нет неопределённой отправки для подтверждения')
        self.set(task, state='sent', error='', sent_at=datetime.now(timezone.utc).isoformat(), mirror='')

    def unbind(self, employee):
        with self.db:
            self.db.execute('DELETE FROM bindings WHERE employee=?', (employee,))
            self.db.execute('DELETE FROM invites WHERE employee=?', (employee,))


def yes(value):
    return str(value).strip().casefold() == 'да'


def text_parts(text):
    # Conservative UTF-16 unit bound, including continuation label.
    parts, chunk, units = [], [], 0
    for char in text:
        size = len(char.encode('utf-16-le')) // 2
        if units + size > 3500:
            parts.append(''.join(chunk))
            chunk, units = [], 0
        chunk.append(char)
        units += size
    if chunk:
        parts.append(''.join(chunk))
    return parts


def message_parts(task, payload):
    text = (f"Новая задача №{task}\n\nЗадача: {payload['text']}\n"
            f"Исполнитель: {payload['name']}\nДедлайн: {payload['due'] or 'Срок не указан'}")
    if payload['due']:
        text += ' (МСК)'
    parts = text_parts(text)
    if len(parts) > 1:
        parts = [f'Задача №{task}, часть {i}/{len(parts)}\n\n{part}' for i, part in enumerate(parts, 1)]
    return parts


FINISH_BUTTON = '✅ Сообщить о завершении задачи'


def task_payload(task):
    return {'employee': str(task['ID сотрудника']).strip(),
            'name': str(task['ФИО']).strip(), 'text': str(task['Задача']).strip(),
            'due': str(task['Дедлайн']).strip()}


def same_task(task, key, payload):
    return (str(task.get('Номер задачи', '')).strip() == key and
            ('due' not in payload or str(task.get('Дедлайн', '')).strip() == payload['due']) and
            all(str(task.get(column, '')).strip() == payload[field]
                for column, field in [('ID сотрудника', 'employee'), ('ФИО', 'name'), ('Задача', 'text')]))


def find_task(tasks, key):
    matches = [(row, task) for row, task in tasks if str(task['Номер задачи']).strip() == key]
    if len(matches) != 1:
        raise ValueError('Задача не найдена или её номер повторяется в таблице.')
    return matches[0]


class CompletionFlow:
    def __init__(self, store, telegram, sheets, admins):
        self.store, self.telegram, self.sheets, self.admins = store, telegram, sheets, admins

    def report(self, uid, key, evidence=None):
        binding = self.store.db.execute('SELECT employee FROM bindings WHERE chat=?', (uid,)).fetchone()
        if not binding:
            raise ValueError('Сначала подключитесь по приглашению администратора.')
        tasks, people = self.sheets.snapshot()
        _, task = find_task(tasks, key)
        payload = task_payload(task)
        person = people.get(binding[0])
        if (payload['employee'] != binding[0] or not person or not yes(person['Активен'])
                or payload['name'] != str(person['ФИО']).strip()):
            raise ValueError('Можно сообщать о завершении только своих активных задач.')
        if task['Статус задачи'] in ('Выполнена', 'Отменена'):
            raise ValueError('Эта задача уже выполнена или отменена.')
        if not self.admins:
            raise ValueError('Администратор не настроен. Отчёт не отправлен.')
        existing = self.store.db.execute(
            "SELECT id FROM completions WHERE task=? AND state IN ('pending','approving','returning')", (key,)).fetchone()
        if existing:
            return 'Отчёт по этой задаче уже ожидает решения администратора.'
        with self.store.db:
            self.store.db.execute('INSERT INTO completions(id,task,employee,chat,payload,evidence) VALUES(?,?,?,?,?,?)',
                (secrets.token_hex(12), key, binding[0], uid, json.dumps(payload, ensure_ascii=False),
                 json.dumps(evidence or [], ensure_ascii=False)))
        self.cycle()
        return f'Отчёт по задаче №{key} сохранён и поставлен на отправку администратору.'

    def cycle(self):
        for record in self.store.db.execute("SELECT * FROM completions WHERE state IN ('pending','approving','returning','approved','returned')").fetchall():
            try:
                if record['state'] == 'pending':
                    self.notify_admins(record)
                else:
                    self.finish(record)
            except APIError as exc:
                if exc.kind == 'fatal':
                    raise
                LOG.warning('Уведомление о завершении не доставлено; повтор позже')
            except Exception:
                LOG.warning('Завершение задачи не синхронизировано; повтор позже')

    def notify_admins(self, record, remember=True):
        payload = json.loads(record['payload'])
        parts = text_parts(f"Сотрудник {payload['name']} сообщил о выполнении задачи №{record['task']}.\n\nЗадача: {payload['text']}")
        notices = json.loads(record['notices'])
        evidence = json.loads(record['evidence'])
        markup = {'inline_keyboard': [
            [{'text': '✅ Подтвердить выполнение задачи', 'callback_data': 'approve:' + record['id']}],
            [{'text': '↩️ Вернуть задачу', 'callback_data': 'return:' + record['id']}]]}
        for admin in sorted(self.admins):
            progress = notices.get(str(admin), 0)
            total = len(parts) + len(evidence) + (1 if evidence else 0)
            for i in range(progress, total):
                if i < len(parts):
                    self.telegram.send(admin, parts[i], reply_markup=markup if not evidence and i == len(parts) - 1 else None)
                elif i < len(parts) + len(evidence):
                    item = evidence[i - len(parts)]
                    if item['kind'] == 'text':
                        self.telegram.send(admin, f"Результат задачи №{record['task']}:\n{item['text']}")
                    else:
                        kind = item['kind']
                        self.telegram.call('sendDocument' if kind == 'document' else 'sendPhoto',
                            {'chat_id': admin, kind: item['file_id'],
                             'caption': f"Результат задачи №{record['task']}\n" + item.get('caption', '')[:350]})
                else:
                    self.telegram.send(admin, f"Решение по задаче №{record['task']}:", reply_markup=markup)
                notices[str(admin)] = i + 1
                if remember:
                    with self.store.db:
                        self.store.db.execute('UPDATE completions SET notices=? WHERE id=?',
                            (json.dumps(notices), record['id']))

    def decide(self, uid, report_id, action, reason=''):
        if uid not in self.admins:
            raise ValueError('Решение может принять только администратор.')
        record = self.store.db.execute('SELECT * FROM completions WHERE id=?', (report_id,)).fetchone()
        if not record:
            raise ValueError('Отчёт не найден.')
        if record['state'] != 'pending':
            return 'Решение по этому отчёту уже принято.'
        tasks, people = self.sheets.snapshot()
        _, task = find_task(tasks, record['task'])
        payload = json.loads(record['payload'])
        if (not same_task(task, record['task'], payload)
                or self.store.chat(record['employee']) != record['chat']
                or task['Статус задачи'] in ('Выполнена', 'Отменена')):
            with self.store.db:
                self.store.db.execute("UPDATE completions SET state='stale' WHERE id=?", (report_id,))
            raise ValueError('Задача или привязка изменена. Этот отчёт устарел; нужен новый отчёт.')
        with self.store.db:
            self.store.db.execute('UPDATE completions SET state=?,reason=? WHERE id=? AND state=\'pending\'',
                ('approving' if action == 'approve' else 'returning', reason, report_id))
        record = self.store.db.execute('SELECT * FROM completions WHERE id=?', (report_id,)).fetchone()
        try:
            self.finish(record)
        except (RuntimeError, APIError):
            return 'Решение сохранено. Синхронизация и уведомление будут повторены автоматически.'
        return 'Выполнение подтверждено.' if action == 'approve' else 'Задача возвращена сотруднику.'

    def finish(self, record):
        state = record['state']
        if state in ('approving', 'returning'):
            tasks, _ = self.sheets.snapshot()
            row, task = find_task(tasks, record['task'])
            if (not same_task(task, record['task'], json.loads(record['payload']))
                    or self.store.chat(record['employee']) != record['chat']
                    or task['Статус задачи'] == 'Отменена'
                    or (state == 'returning' and task['Статус задачи'] == 'Выполнена')):
                with self.store.db:
                    self.store.db.execute("UPDATE completions SET state='stale' WHERE id=?", (record['id'],))
                raise ValueError('Задача изменена; решение не применено.')
            if state == 'approving':
                self.sheets.complete(row, record['task'], json.loads(record['payload']))
            state = 'approved' if state == 'approving' else 'returned'
            with self.store.db:
                self.store.db.execute('UPDATE completions SET state=? WHERE id=?', (state, record['id']))
        if record['result_progress']:
            return
        if self.store.chat(record['employee']) != record['chat']:
            return
        text = (f"✅ Администратор подтвердил выполнение задачи №{record['task']}. Статус: Выполнена."
                if state == 'approved' else
                f"↩️ Администратор вернул задачу №{record['task']}. Задача ещё не считается выполненной. После доработки сообщите о завершении повторно.")
        if state == 'returned' and record['reason']:
            text += '\n\nЧто исправить: ' + record['reason']
        self.telegram.send(record['chat'], text)
        with self.store.db:
            self.store.db.execute('UPDATE completions SET result_progress=1 WHERE id=?', (record['id'],))


def handle_completion_callback(callback, store, telegram, sheets, admins):
    uid = callback.get('from', {}).get('id')
    message = callback.get('message', {})
    chat = message.get('chat', {})
    data = callback.get('data', '')
    action, _, report_id = data.partition(':')
    if action == 'finish':
        reply = 'Откройте бота в личном чате.'
        if chat.get('type') == 'private' and chat.get('id') == uid:
            try:
                tasks, _ = sheets.snapshot()
                keys = [str(task['Номер задачи']).strip() for _, task in tasks
                        if hashlib.sha256(str(task['Номер задачи']).strip().encode()).hexdigest()[:32] == report_id]
                if len(keys) != 1:
                    raise ValueError('Задача не найдена или её номер повторяется.')
                reply = CompletionFlow(store, telegram, sheets, admins).report(uid, keys[0])
            except ValueError as exc:
                reply = str(exc)
            except RuntimeError:
                reply = 'Таблица недоступна. Попробуйте позже.'
        telegram.call('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': reply[:200], 'show_alert': True})
        return
    if action not in ('approve', 'return'):
        return
    reply = 'Решение может принять только администратор в личном чате.'
    if uid in admins and chat.get('type') == 'private' and chat.get('id') == uid:
        try:
            reply = CompletionFlow(store, telegram, sheets, admins).decide(uid, report_id, action)
        except ValueError as exc:
            reply = str(exc)
        except RuntimeError:
            reply = 'Таблица недоступна. Попробуйте нажать кнопку позже.'
    telegram.call('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': reply[:200], 'show_alert': True})


class Engine:
    def __init__(self, store, telegram, sheets):
        self.store, self.telegram, self.sheets = store, telegram, sheets

    def cycle(self):
        tasks, people = self.sheets.snapshot()
        counts = Counter(str(t['Номер задачи']).strip() for _, t in tasks)
        for row, task in tasks:
            key = str(task['Номер задачи']).strip()
            if not key or counts[key] != 1 or len(key) > 40:
                LOG.warning('Пропущена строка %s: пустой, повторный или слишком длинный номер', row)
                continue
            try:
                self.process(row, key, task, people)
            except APIError as exc:
                if exc.kind == 'fatal':
                    raise
                LOG.error('Ошибка Telegram при обработке задачи %s', key)
            except Exception:
                LOG.error('Не удалось обработать/синхронизировать задачу %s; повтор в следующем цикле', key)

    def process(self, row, key, task, people):
        record = self.store.get(key)
        employee = str(task['ID сотрудника']).strip()
        payload = {'employee': employee, 'text': str(task['Задача']).strip(),
                   'name': str(task['ФИО']).strip(), 'due': str(task['Дедлайн']).strip()}
        ready = yes(task['Готова к отправке']) and task['Статус задачи'] not in ('Отменена', 'Выполнена')
        valid = bool(employee and payload['text'] and payload['name'])
        person = people.get(employee)
        active = person is not None and yes(person['Активен'])
        if not record:
            if not ready:
                return
            if not valid or not active or str(person['ФИО']).strip() != payload['name']:
                self.sheets.mirror(row, key, 'Ошибка', '', 'Проверьте поля, ID, ФИО и активность сотрудника')
                return
            # Existing sent rows may come from a previous deployment. Never resend blindly.
            if task['Статус уведомления'] == 'Отправлено':
                self.store.enqueue(key, payload)
                self.store.set(key, state='uncertain', error='Таблица сообщает об отправке, но в базе нет истории; нужна проверка')
            else:
                self.store.enqueue(key, payload)
            record = self.store.get(key)
        previous = json.loads(record['payload'])
        # Before the first successful chunk the current task may be updated safely.
        if record['state'] == 'pending' and record['progress'] == 0 and previous != payload:
            self.store.set(key, payload=json.dumps(payload, ensure_ascii=False))
            record = self.store.get(key)
            previous = payload
        if record['state'] == 'pending' and ready and valid and active:
            if previous != payload or str(person['ФИО']).strip() != payload['name']:
                self.store.set(key, state='uncertain', error='Задача изменена во время отправки; нужна проверка')
            elif self.store.chat(employee) is None:
                self.mirror(row, key, 'Сотрудник не подключён', '', '')
                return
            elif record['retry_at'] <= time.time():
                self.deliver(key, previous, record)
        record = self.store.get(key)
        if record['state'] == 'sent':
            self.mirror(row, key, 'Отправлено', record['sent_at'], '')
        elif record['state'] in ('uncertain', 'permanent'):
            self.mirror(row, key, 'Ошибка', '', record['error'])
        elif ready:
            self.mirror(row, key, 'Ожидает', '', record['error'])

    def deliver(self, key, payload, record):
        parts = message_parts(key, payload)
        progress = record['progress']
        destination = self.store.chat(payload['employee'])
        last_message = self.store.db.execute('SELECT chat FROM message_log WHERE task=? LIMIT 1', (key,)).fetchone()
        if progress and last_message and last_message[0] != destination:
            self.store.set(key, state='uncertain', error='Аккаунт изменён после частичной отправки; нужна ручная проверка')
            return
        attempts = record['attempts'] + 1
        self.store.set(key, state='sending', attempts=attempts)
        try:
            for i in range(progress, len(parts)):
                markup = {'inline_keyboard': [[{
                    'text': f'✅ Сообщить о завершении задачи №{key}',
                    'callback_data': 'finish:' + hashlib.sha256(key.encode()).hexdigest()[:32]}]]}
                markup['inline_keyboard'].insert(0, [{'text': '✅ Принять задачу',
                    'callback_data': 'accept:' + hashlib.sha256(key.encode()).hexdigest()[:32]}])
                message_id = self.telegram.send(destination, parts[i],
                    reply_markup=markup if i == len(parts) - 1 else None)
                # Acknowledgement and progress are committed together.
                with self.store.db:
                    self.store.db.execute('INSERT OR REPLACE INTO message_log VALUES(?,?,?,?,?)',
                                          (key, i, destination, message_id, datetime.now(timezone.utc).isoformat()))
                    self.store.db.execute('UPDATE deliveries SET progress=? WHERE task=?', (i + 1, key))
                time.sleep(0.15)
        except APIError as exc:
            if exc.kind == 'fatal':
                self.store.set(key, state='pending')
                raise
            state = 'pending' if exc.kind == 'retry' else exc.kind
            self.store.set(key, state=state, error=str(exc),
                           retry_at=time.time() + max(exc.retry_after, min(3600, 30 * 2 ** min(attempts, 7))))
            return
        self.store.set(key, state='sent', error='', sent_at=datetime.now(timezone.utc).isoformat())
        LOG.info('Уведомление по задаче %s отправлено', key)

    def mirror(self, row, key, status, sent_at, error):
        signature = json.dumps([status, sent_at, error])
        if self.store.get(key)['mirror'] != signature:
            self.sheets.mirror(row, key, status, sent_at, error)
            self.store.set(key, mirror=signature)


def handle_update(update, store, telegram, sheets, admins, username):
    from workflow import Workflow
    if Workflow(store, telegram, sheets, admins).handle(update):
        return
    if update.get('callback_query'):
        handle_completion_callback(update['callback_query'], store, telegram, sheets, admins)
        return
    message = update.get('message', {})
    chat = message.get('chat', {})
    if chat.get('type') != 'private' or not message.get('text'):
        return
    uid = message.get('from', {}).get('id')
    if uid != chat.get('id'):
        return
    text = message['text'].strip()
    if not text:
        return
    fields = text.split(maxsplit=1)
    command = fields[0].split('@')[0]
    argument = fields[1].strip() if len(fields) > 1 else ''
    if text == FINISH_BUTTON:
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (f'finish:{uid}', '1'))
        telegram.send(uid, 'Введите номер завершённой задачи. Например: 12. Для отмены — /cancel.')
        return
    waiting = store.db.execute('SELECT value FROM settings WHERE key=?', (f'finish:{uid}',)).fetchone()
    if command == '/done' or (waiting and not text.startswith('/')):
        key = argument if command == '/done' else text
        try:
            reply = CompletionFlow(store, telegram, sheets, admins).report(uid, key)
            with store.db:
                store.db.execute('DELETE FROM settings WHERE key=?', (f'finish:{uid}',))
        except ValueError as exc:
            reply = str(exc)
        telegram.send(uid, reply)
        return
    if waiting and text.startswith('/'):
        with store.db:
            store.db.execute('DELETE FROM settings WHERE key=?', (f'finish:{uid}',))
        if command == '/cancel':
            telegram.send(uid, 'Ввод номера отменён.')
            return
    if text == '📊 Открыть таблицу':
        command, argument = '/table', ''
    elif text == '👥 Сотрудники':
        command, argument = '/employees', ''
    markup = None
    if uid in admins:
        markup = {'keyboard': [[{'text': '📊 Открыть таблицу'}, {'text': '👥 Сотрудники'}], [{'text': FINISH_BUTTON}]],
                  'resize_keyboard': True, 'is_persistent': True}
    reply = 'Для подключения нужна персональная ссылка от администратора.'
    if command == '/id':
        reply = f'Ваш Telegram ID: {uid}'
    elif command == '/start' and argument:
        digest = hashlib.sha256(argument.encode()).hexdigest()
        invite = store.db.execute('SELECT employee FROM invites WHERE hash=?', (digest,)).fetchone()
        _, people = sheets.snapshot()
        if invite and invite[0] in people and yes(people[invite[0]]['Активен']):
            try:
                employee = store.bind(argument, uid)
                reply = f"Вы подключены: {people[employee]['ФИО']}. Новые поручения будут приходить сюда."
            except ValueError as exc:
                reply = str(exc)
        else:
            reply = 'Приглашение недействительно или сотрудник неактивен.'
    elif uid in admins:
        if command == '/table':
            spreadsheet_id = os.getenv('GOOGLE_SPREADSHEET_ID', '').strip()
            if spreadsheet_id:
                reply = 'Открыть общую таблицу поручений:'
                markup = {'inline_keyboard': [[{'text': '📊 Открыть таблицу',
                           'url': f'https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit'}]]}
            else:
                reply = 'ID таблицы не настроен. Проверьте GOOGLE_SPREADSHEET_ID.'
        elif command == '/employees':
            _, people = sheets.snapshot()
            rows = [f"{person['ФИО']} — ID сотрудника: {employee}" +
                    ('' if yes(person['Активен']) else ' (неактивен)')
                    for employee, person in sorted(people.items(),
                        key=lambda item: (str(item[1]['ФИО']).casefold(), item[0]))]
            reply = f'Сотрудники: {len(rows)}\n\n' + '\n'.join(rows) if rows else 'Список сотрудников пока пуст. Добавьте сотрудников на лист «Сотрудники».'
        elif command == '/invite' and argument:
            _, people = sheets.snapshot()
            if argument in people and yes(people[argument]['Активен']):
                code = store.invite(argument)
                reply = f'Приглашение на 24 часа:\nhttps://t.me/{username}?start={code}'
            else:
                reply = 'Активный сотрудник с таким ID не найден.'
        elif command == '/errors':
            rows = store.db.execute("SELECT task,error FROM deliveries WHERE state IN ('uncertain','permanent') ORDER BY rowid DESC LIMIT 20").fetchall()
            reply = '\n'.join(f"№{r['task']}: {r['error'][:100]}" for r in rows) or 'Ошибок отправки нет.'
        elif command == '/retry' and argument:
            try:
                store.retry(argument)
                reply = 'Повтор разрешён. Если результат предыдущей отправки был неизвестен, возможно дублирование.'
            except ValueError as exc:
                reply = str(exc)
        elif command == '/sent' and argument:
            try:
                store.mark_sent(argument)
                reply = 'Отправка подтверждена администратором. Повторного сообщения не будет.'
            except ValueError as exc:
                reply = str(exc)
        elif command == '/unbind' and argument:
            store.unbind(argument)
            reply = 'Привязка и приглашения удалены. Для подключения создайте новую ссылку.'
        else:
            reply = 'Меню администратора\n\n/table — открыть таблицу\n/employees — ФИО и ID сотрудников\n/invite ID — приглашение\n/errors — ошибки\n/retry НОМЕР — повтор после проверки\n/sent НОМЕР — подтвердить неопределённую отправку\n/unbind ID — отвязать\n/id — ваш ID'
    else:
        binding = store.db.execute('SELECT employee FROM bindings WHERE chat=?', (uid,)).fetchone()
        if binding:
            reply = f'Вы подключены как сотрудник {binding[0]}. Здесь приходят новые поручения.'
    parts = text_parts(reply)
    from workflow import menu
    if markup is not None and 'keyboard' in markup:
        markup = menu(uid in admins)
    if uid not in admins and store.db.execute('SELECT 1 FROM bindings WHERE chat=?', (uid,)).fetchone():
        markup = menu(False)
    for i, part in enumerate(parts):
        if i == len(parts) - 1 and markup is not None:
            telegram.send(uid, part, reply_markup=markup)
        else:
            telegram.send(uid, part)


def main():
    load_env()
    parser = argparse.ArgumentParser(description='Task notification bot')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('run')
    sub.add_parser('check')
    invite = sub.add_parser('invite')
    invite.add_argument('employee')
    invite.add_argument('--username', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    database = os.getenv('BOT_DATABASE', 'data/bot.sqlite3')
    # OS lock prevents concurrent senders and startup recovery against a live sender.
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    lock = open(database + '.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        parser.exit(1, 'Уже работает другой процесс с этой базой.\n')
    store = Store(database)
    if args.command == 'invite':
        print(f'https://t.me/{args.username}?start={store.invite(args.employee)}')
        return
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    creds = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '')
    if not token or not Path(creds).is_file():
        parser.exit(1, 'Заполните .env и сохраните ключ сервисного аккаунта.\n')
    interval = int(os.getenv('POLL_INTERVAL', '60'))
    max_rows = int(os.getenv('MAX_ROWS', '10000'))
    if interval < 10 or not 100 <= max_rows <= 100000:
        parser.exit(1, 'POLL_INTERVAL >= 10; MAX_ROWS от 100 до 100000.\n')
    telegram = Telegram(token)
    sheets = Sheets(creds, os.getenv('GOOGLE_SPREADSHEET_ID', ''),
                    os.getenv('TASKS_SHEET', 'Задачи'), os.getenv('EMPLOYEES_SHEET', 'Сотрудники'), max_rows)
    admins = {int(x.strip()) for x in os.getenv('TELEGRAM_ADMIN_IDS', '').split(',') if x.strip()}
    identity = telegram.call('getMe')
    info = sheets.metadata()
    titles = {x['properties']['title']: x['properties']['gridProperties']['rowCount'] for x in info['sheets']}
    if any(s not in titles or titles[s] > max_rows for s in (sheets.task_sheet, sheets.people_sheet)):
        parser.exit(1, 'Проверьте названия листов и MAX_ROWS; нельзя молча пропускать строки.\n')
    if info.get('properties', {}).get('timeZone') != 'Europe/Moscow':
        parser.exit(1, 'Установите часовой пояс таблицы Europe/Moscow (Москва).\n')
    tasks, people = sheets.snapshot()
    print(f"Подключён @{identity['username']}. Задач: {len(tasks)}; сотрудников: {len(people)}. Администраторов: {len(admins)}.")
    if args.command == 'check':
        return
    engine = Engine(store, telegram, sheets)
    completions = CompletionFlow(store, telegram, sheets, admins)
    from workflow import Workflow
    workflow = Workflow(store, telegram, sheets, admins)
    next_cycle = 0
    failures = 0
    try:
        while True:
            if time.monotonic() >= next_cycle:
                try:
                    engine.cycle()
                    workflow.cycle()
                    completions.cycle()
                    failures = 0
                except APIError as exc:
                    if exc.kind == 'fatal':
                        raise
                    failures += 1
                    LOG.error('Сбой цикла отправки; повтор позже')
                except Exception:
                    failures += 1
                    LOG.error('Таблица недоступна или некорректна; отправка приостановлена')
                next_cycle = time.monotonic() + min(3600, interval * 2 ** min(failures, 5))
            try:
                updates = telegram.call('getUpdates', {'offset': store.offset(), 'timeout': 15, 'limit': 20, 'allowed_updates': ['message', 'callback_query']})
                for update in updates:
                    try:
                        handle_update(update, store, telegram, sheets, admins, identity['username'])
                    except APIError as exc:
                        if exc.kind == 'fatal':
                            raise
                        LOG.warning('Ответ на команду не отправлен; пользователь может повторить команду')
                    except Exception:
                        LOG.warning('Команда не обработана; пользователь может повторить её позже')
                    store.offset(update['update_id'] + 1)
            except APIError as exc:
                if exc.kind == 'fatal':
                    raise
                LOG.warning('Ошибка получения команд Telegram; повтор позже')
                time.sleep(max(5, min(exc.retry_after, 60)))
    except KeyboardInterrupt:
        LOG.info('Бот остановлен')


if __name__ == '__main__':
    try:
        main()
    except APIError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit('Ошибка запуска. Проверьте зависимости, .env и доступ к сервисам.') from None
