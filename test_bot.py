import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import APIError, Engine, Sheets, Store, TASK_HEADERS, PEOPLE_HEADERS, Telegram, handle_update, message_parts


def task(**changes):
    value = dict(zip(TASK_HEADERS, ['1', '02.10.2026 10:00', 'Подготовить договор',
                    'Иванов Сергей', '06.10.2026 15:00', 'Новая', 'EMP001', 'Да', '', '', '']))
    value.update(changes)
    return value


class FakeSheets:
    def __init__(self):
        self.tasks = [(2, task())]
        self.people = {'EMP001': {'ФИО': 'Иванов Сергей', 'Активен': 'Да'}}
        self.writes = []
        self.fail_write = False

    def snapshot(self):
        return self.tasks, self.people

    def mirror(self, *args):
        if self.fail_write:
            raise RuntimeError('offline')
        self.writes.append(args)


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.errors = []
        self.markups = []

    def send(self, chat, text, reply_markup=None):
        if self.errors:
            error = self.errors.pop(0)
            if error:
                raise error
        self.messages.append((chat, text))
        self.markups.append(reply_markup)
        return len(self.messages)


class BotTests(unittest.TestCase):
    def menu_update(self, text, uid=10):
        handle_update({'message': {'chat': {'id': uid, 'type': 'private'},
                       'from': {'id': uid}, 'text': text}}, self.store, self.tg,
                      self.sheets, {10}, 'test_bot')

    def test_admin_menu_and_sheet_button(self):
        self.menu_update('/start')
        self.assertIn('👥 Сотрудники', str(self.tg.markups[-1]))
        with patch.dict('os.environ', {'GOOGLE_SPREADSHEET_ID': 'example-sheet'}):
            self.menu_update('📊 Открыть таблицу')
        self.assertEqual(self.tg.markups[-1]['inline_keyboard'][0][0]['url'],
                         'https://docs.google.com/spreadsheets/d/example-sheet/edit')

    def test_employee_list_admin_only(self):
        for text in ('/employees', '👥 Сотрудники', '/table', '📊 Открыть таблицу'):
            with patch.object(self.sheets, 'snapshot', side_effect=AssertionError('Unauthorized read')):
                self.menu_update(text, uid=20)
            self.assertNotIn('Иванов', self.tg.messages[-1][1])
            self.assertIsNone(self.tg.markups[-1])
        self.menu_update('👥 Сотрудники')
        self.assertIn('Иванов Сергей — ID сотрудника: EMP001', self.tg.messages[-1][1])

    def test_large_and_empty_employee_list(self):
        self.sheets.people = {}
        self.menu_update('/employees')
        self.assertIn('пока пуст', self.tg.messages[-1][1])
        self.sheets.people = {f'EMP{i:04}': {'ФИО': '😀' * 50 + str(i), 'Активен': 'Нет'} for i in range(100)}
        self.tg.messages.clear()
        self.menu_update('/employees')
        self.assertGreater(len(self.tg.messages), 1)
        self.assertTrue(all(len(text.encode('utf-16-le')) // 2 <= 3500 for _, text in self.tg.messages))
        self.assertIn('EMP0099', ''.join(text for _, text in self.tg.messages))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'db.sqlite3')
        self.store = Store(self.path)
        self.tg = FakeTelegram()
        self.sheets = FakeSheets()
        self.engine = Engine(self.store, self.tg, self.sheets)
        self.sleep = patch('bot.time.sleep').start()

    def tearDown(self):
        patch.stopall()
        self.store.db.close()
        self.tmp.cleanup()

    def bind(self, employee='EMP001', chat=10):
        return self.store.bind(self.store.invite(employee), chat)

    def test_send_once_and_restart(self):
        self.bind()
        self.engine.cycle()
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)
        self.store.db.close()
        self.store = Store(self.path)
        Engine(self.store, self.tg, self.sheets).cycle()
        self.assertEqual(len(self.tg.messages), 1)
        self.assertEqual(self.store.get('1')['state'], 'sent')

    def test_missing_binding_waits_then_sends(self):
        self.engine.cycle()
        self.assertEqual(self.sheets.writes[-1][2], 'Сотрудник не подключён')
        self.bind()
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)

    def test_unready_not_sent(self):
        self.bind()
        self.sheets.tasks[0][1]['Готова к отправке'] = 'Нет'
        self.engine.cycle()
        self.assertEqual(self.tg.messages, [])

    def test_duplicate_ids_all_skipped(self):
        self.bind()
        self.sheets.tasks.append((3, task()))
        self.engine.cycle()
        self.assertEqual(self.tg.messages, [])

    def test_error_in_one_row_does_not_stop_another(self):
        self.bind()
        self.sheets.tasks = [(2, task(**{'ID сотрудника': 'missing'})),
                             (3, task(**{'Номер задачи': '2'}))]
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)
        self.assertEqual(self.sheets.writes[0][2], 'Ошибка')

    def test_inactive_or_wrong_name_not_sent(self):
        self.bind()
        self.sheets.people['EMP001']['Активен'] = 'Нет'
        self.engine.cycle()
        self.sheets.people['EMP001']['Активен'] = 'Да'
        self.sheets.tasks[0][1]['ФИО'] = 'Другой человек'
        self.engine.cycle()
        self.assertEqual(self.tg.messages, [])

    def test_cancelled_not_sent(self):
        self.bind()
        self.sheets.tasks[0][1]['Статус задачи'] = 'Отменена'
        self.engine.cycle()
        self.assertEqual(self.tg.messages, [])

    def test_sheet_write_failure_does_not_resend(self):
        self.bind()
        self.sheets.fail_write = True
        self.engine.cycle()
        self.sheets.fail_write = False
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)
        self.assertEqual(self.sheets.writes[-1][2], 'Отправлено')

    def test_row_sort_does_not_resend(self):
        self.bind()
        self.engine.cycle()
        self.sheets.tasks = [(9, task())]
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)

    def test_ambiguous_send_requires_admin_decision(self):
        self.bind()
        self.tg.errors = [APIError('uncertain', 'timeout')]
        self.engine.cycle()
        self.engine.cycle()
        self.assertEqual(self.store.get('1')['state'], 'uncertain')
        self.assertEqual(self.tg.messages, [])
        self.store.mark_sent('1')
        self.engine.cycle()
        self.assertEqual(self.store.get('1')['state'], 'sent')
        self.assertEqual(self.tg.messages, [])

    def test_rate_limit_retry_is_scheduled(self):
        self.bind()
        self.tg.errors = [APIError('retry', 'rate', 120)]
        self.engine.cycle()
        self.assertGreaterEqual(self.store.get('1')['retry_at'], time.time() + 119)
        self.engine.cycle()
        self.assertEqual(self.tg.messages, [])
        self.store.set('1', retry_at=0)
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)

    def test_permanent_error_not_retried_without_admin(self):
        self.bind()
        self.tg.errors = [APIError('permanent', 'blocked')]
        self.engine.cycle()
        self.engine.cycle()
        self.assertEqual(self.store.get('1')['state'], 'permanent')
        self.store.retry('1')
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)

    def test_crash_during_send_recovers_as_uncertain(self):
        self.engine.cycle()
        self.store.set('1', state='sending')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get('1')['state'], 'uncertain')

    def test_invite_single_use_and_idempotent_same_account(self):
        code = self.store.invite('EMP001')
        self.assertEqual(self.store.bind(code, 10), 'EMP001')
        self.assertEqual(self.store.bind(code, 10), 'EMP001')
        with self.assertRaises(ValueError):
            self.store.bind(code, 20)

    def test_new_invite_revokes_old(self):
        old = self.store.invite('EMP001')
        new = self.store.invite('EMP001')
        with self.assertRaises(ValueError):
            self.store.bind(old, 10)
        self.assertEqual(self.store.bind(new, 10), 'EMP001')

    def test_invite_expires(self):
        code = self.store.invite('EMP001', hours=-1)
        with self.assertRaises(ValueError):
            self.store.bind(code, 10)

    def test_one_account_cannot_bind_two_employees(self):
        self.bind()
        with self.assertRaises(ValueError):
            self.bind('EMP002', 10)

    def test_unbind_revokes_even_used_invite(self):
        code = self.store.invite('EMP001')
        self.store.bind(code, 10)
        self.store.unbind('EMP001')
        with self.assertRaises(ValueError):
            self.store.bind(code, 10)

    def test_existing_sent_row_without_db_never_resends(self):
        self.bind()
        self.sheets.tasks[0][1]['Статус уведомления'] = 'Отправлено'
        self.engine.cycle()
        self.assertEqual(self.tg.messages, [])
        self.assertEqual(self.store.get('1')['state'], 'uncertain')

    def test_long_unicode_task_chunks_fit(self):
        p = {'text': '😀' * 6000, 'name': 'Иванов Сергей', 'due': ''}
        parts = message_parts('1', p)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(x.encode('utf-16-le')) // 2 <= 4096 for x in parts))
        self.assertEqual(sum(x.count('😀') for x in parts), 6000)

    def test_partial_message_retry_resumes(self):
        self.bind()
        self.sheets.tasks[0][1]['Задача'] = 'Я' * 6000
        self.tg.errors = [None, APIError('retry', 'rate')]
        self.engine.cycle()
        self.assertEqual(self.store.get('1')['progress'], 1)
        self.store.set('1', retry_at=0)
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 2)
        self.assertEqual(self.store.get('1')['state'], 'sent')

    def test_edited_task_after_send_never_resends(self):
        self.bind()
        self.engine.cycle()
        self.sheets.tasks[0][1]['Задача'] = 'Новая формулировка'
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)

    def test_partial_task_does_not_continue_to_new_account(self):
        self.bind()
        self.sheets.tasks[0][1]['Задача'] = 'Я' * 6000
        self.tg.errors = [None, APIError('retry', 'rate')]
        self.engine.cycle()
        self.store.unbind('EMP001')
        self.bind(chat=20)
        self.store.set('1', retry_at=0)
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)
        self.assertEqual(self.store.get('1')['state'], 'uncertain')

    def test_partial_task_change_requires_review(self):
        self.bind()
        self.sheets.tasks[0][1]['Задача'] = 'Я' * 6000
        self.tg.errors = [None, APIError('retry', 'rate')]
        self.engine.cycle()
        self.sheets.tasks[0][1]['Задача'] = 'Изменено после первой части'
        self.store.set('1', retry_at=0)
        self.engine.cycle()
        self.assertEqual(len(self.tg.messages), 1)
        self.assertEqual(self.store.get('1')['state'], 'uncertain')

    def test_google_read_and_mirror_shape(self):
        sheets = Sheets.__new__(Sheets)
        sheets.task_sheet, sheets.people_sheet, sheets.max_rows = 'Задачи', 'Сотрудники', 10000
        responses = [
            {'properties': {'timeZone': 'Europe/Moscow'}, 'sheets': [
                {'properties': {'title': name, 'gridProperties': {'rowCount': 101}}}
                for name in ('Задачи', 'Сотрудники')]},
            {'values': [TASK_HEADERS, list(task().values())]},
            {'values': [PEOPLE_HEADERS, ['EMP001', 'Иванов Сергей', '', '', 'Да']]},
            {'values': [['1']]}, {}]
        with patch.object(sheets, 'request', side_effect=responses) as request:
            rows, people = sheets.snapshot()
            self.assertEqual(rows[0][0], 2)
            self.assertIn('EMP001', people)
            sheets.mirror(2, '1', 'Отправлено', '2026-10-02T10:00:00+00:00', '')
            body = request.call_args.kwargs['json']
            self.assertEqual(body['valueInputOption'], 'RAW')
            self.assertEqual(body['data'][0]['range'], "'Задачи'!I2:K2")
            self.assertIsInstance(body['data'][0]['values'][0][1], float)

    def test_moved_row_blocks_google_write(self):
        sheets = Sheets.__new__(Sheets)
        sheets.task_sheet = 'Задачи'
        with patch.object(sheets, 'request', return_value={'values': [['another-id']]}) as request:
            with self.assertRaises(RuntimeError):
                sheets.mirror(2, '1', 'Отправлено', '', '')
            self.assertEqual(request.call_count, 1)

    def test_google_header_mismatch_rejected(self):
        sheets = Sheets.__new__(Sheets)
        sheets.max_rows = 10000
        with patch.object(sheets, 'request', return_value={'values': [['Wrong header']]}):
            with self.assertRaises(RuntimeError):
                sheets.read('Задачи', 'K', TASK_HEADERS)

    def test_groups_ignored_and_admin_commands_restricted(self):
        handle_update({'message': {'chat': {'id': 10, 'type': 'group'},
                                  'from': {'id': 10}, 'text': '/invite EMP001'}},
                      self.store, self.tg, self.sheets, {10}, 'test_bot')
        self.assertEqual(self.tg.messages, [])
        handle_update({'message': {'chat': {'id': 20, 'type': 'private'},
                                  'from': {'id': 20}, 'text': '/invite EMP001'}},
                      self.store, self.tg, self.sheets, {10}, 'test_bot')
        self.assertNotIn('https://t.me', self.tg.messages[-1][1])

    def test_transport_failure_is_ambiguous_and_redacted(self):
        tg = Telegram('private-token')
        with patch('bot.urllib.request.urlopen', side_effect=OSError('private-token')):
            with self.assertRaises(APIError) as caught:
                tg.send(10, 'test')
        self.assertEqual(caught.exception.kind, 'uncertain')
        self.assertNotIn('private-token', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
