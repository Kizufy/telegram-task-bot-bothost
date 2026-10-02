import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from bot import APIError, CompletionFlow, Engine, MOSCOW, Sheets, Store, TASK_HEADERS, handle_update
from test_bot import FakeSheets, FakeTelegram, task
from workflow import Workflow, deadline, input_deadline, serial, token, revision, MY, PEOPLE, OVERVIEW, REVIEW


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'state.db')
        self.store = Store(self.path)
        self.tg, self.sheets = FakeTelegram(), FakeSheets()
        self.store.bind(self.store.invite('EMP001'), 20)
        self.ui = Workflow(self.store, self.tg, self.sheets, {10})
        self.sleep = patch('bot.time.sleep').start()

    def tearDown(self):
        patch.stopall()
        self.store.db.close()
        self.tmp.cleanup()

    def message(self, text='', uid=20, **extra):
        handle_update({'message': {'from': {'id': uid}, 'chat': {'id': uid, 'type': 'private'}, 'text': text, **extra}},
            self.store, self.tg, self.sheets, {10}, 'test_bot')

    def click(self, data, uid=20):
        handle_update({'callback_query': {'id': 'query', 'from': {'id': uid},
            'message': {'chat': {'id': uid, 'type': 'private'}}, 'data': data}},
            self.store, self.tg, self.sheets, {10}, 'test_bot')

    def sent(self):
        Engine(self.store, self.tg, self.sheets).cycle()
        self.ui.observe(*self.sheets.snapshot())
        self.tg.messages.clear()

    def request(self):
        self.ui.begin_move(20, '1')
        self.message('10.10.2099 18:00')
        self.message('Ожидаю исходные данные')
        return self.store.db.execute('SELECT * FROM deadline_requests').fetchone()

    def edit(self, field, value):
        self.click('edit:' + token('1'), uid=10)
        d = self.ui.dialog(10)
        self.click('field:' + field + ':' + d['nonce'], uid=10)
        self.message(value, uid=10)
        return self.ui.dialog(10)

    def test_employee_buttons_open_only_selected_employee_tasks(self):
        self.sheets.people['EMP002'] = {'ФИО': 'Петров Алексей', 'Активен': 'Да'}
        self.sheets.tasks.append((3, task(**{'Номер задачи': '2', 'ФИО': 'Петров Алексей', 'ID сотрудника': 'EMP002'})))
        self.message(PEOPLE, uid=10)
        self.assertIn('staff:', str(self.tg.markups[-1]))
        self.click('staff:' + token('EMP002') + ':0', uid=10)
        self.assertIn('№2', self.tg.messages[-1][1])
        self.assertNotIn('№1', self.tg.messages[-1][1])
        self.assertIn('К сотрудникам', str(self.tg.markups[-1]))

    def test_employee_cannot_read_other_people_or_admin_lists(self):
        for callback in ('people:0', 'staff:' + token('EMP001') + ':0', 'list:open:0', 'list:review:0', 'reminders', 'edit:' + token('1')):
            with patch.object(self.sheets, 'snapshot', side_effect=AssertionError('Unauthorized read')):
                self.click(callback)
            self.assertIn('администратору', self.tg.messages[-1][1])

    def test_my_tasks_filters_others_and_closed_tasks_paginates(self):
        self.sheets.tasks += [(i+2, task(**{'Номер задачи': str(i)})) for i in range(2, 10)]
        self.sheets.tasks += [(20, task(**{'Номер задачи': 'closed', 'Статус задачи': 'Выполнена'})),
                              (21, task(**{'Номер задачи': 'other', 'ID сотрудника': 'EMP002'}))]
        self.message(MY)
        self.assertIn('Мои открытые задачи: 9', self.tg.messages[-1][1])
        self.assertNotIn('other', self.tg.messages[-1][1])
        self.assertNotIn('closed', self.tg.messages[-1][1])
        self.assertIn('list:mine:1', str(self.tg.markups[-1]))
        self.click('list:mine:1')
        self.assertIn('Страница 2', self.tg.messages[-1][1])

    def test_long_cards_are_chunked_and_buttons_fit_telegram(self):
        self.sheets.tasks[0][1]['Задача'] = '😀' * 8000
        self.sheets.people['X'*200] = {'ФИО': 'Очень длинное имя ' * 30, 'Активен': 'Да'}
        self.click('card:' + token('1'))
        self.message(PEOPLE, uid=10)
        for _, text in self.tg.messages:
            self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 3500)
        for markup in self.tg.markups:
            for row in (markup or {}).get('inline_keyboard', []):
                for b in row:
                    self.assertLessEqual(len(b['callback_data'].encode()), 64)

    def test_acceptance_idempotent_and_ownership_guarded(self):
        self.click('accept:' + token('1'), uid=99)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM task_acceptances').fetchone()[0], 0)
        self.click('accept:' + token('1'))
        self.click('accept:' + token('1'))
        notices = [text for uid, text in self.tg.messages if uid == 10]
        self.assertEqual(len(notices), 1)
        self.click('card:' + token('1'))
        self.assertIn('Принята сотрудником: Да', self.tg.messages[-1][1])

    def test_report_collects_photo_document_link_and_comment(self):
        self.click('finish:' + token('1'))
        self.message(document={'file_id': 'doc'})
        self.message(photo=[{'file_id': 'small'}, {'file_id': 'large'}], caption='Фото результата')
        self.message('https://example.org/result Готовый договор')
        self.assertEqual(len(self.ui.dialog(20)['evidence']), 3)
        self.click('submit:' + self.ui.dialog(20)['nonce'])
        report = self.store.db.execute('SELECT * FROM completions').fetchone()
        self.assertEqual(len(json.loads(report['evidence'])), 3)
        self.assertTrue(any(method == 'sendDocument' and payload['document'] == 'doc' for method, payload in self.tg.calls))
        self.assertTrue(any(method == 'sendPhoto' and payload['photo'] == 'large' for method, payload in self.tg.calls))
        self.assertIn('approve:', str(self.tg.markups[-2]))

    def test_report_reopen_preserves_all_admin_delivery_progress(self):
        reportflow = CompletionFlow(self.store, self.tg, self.sheets, {10, 11})
        reportflow.report(20, '1', [{'kind': 'text', 'text': 'Готово'}])
        report = self.store.db.execute('SELECT * FROM completions').fetchone()
        self.ui.show_report(10, report['id'])
        refreshed = self.store.db.execute('SELECT notices FROM completions').fetchone()[0]
        self.assertEqual(refreshed, report['notices'])
        count = len(self.tg.messages)
        reportflow.cycle()
        self.assertEqual(count, len(self.tg.messages))

    def test_return_reason_is_required_and_sent_to_employee(self):
        CompletionFlow(self.store, self.tg, self.sheets, {10}).report(20, '1')
        report = self.store.db.execute('SELECT id FROM completions').fetchone()[0]
        self.click('return:' + report, uid=10)
        self.assertEqual(self.store.db.execute('SELECT state FROM completions').fetchone()[0], 'pending')
        self.message('Уточните сумму в пункте 3', uid=10)
        self.assertTrue(any('Что исправить: Уточните сумму в пункте 3' in text for uid, text in self.tg.messages if uid == 20))
        self.assertEqual(self.sheets.tasks[0][1]['Статус задачи'], 'Новая')

    def test_cancel_and_expired_report_submission_do_not_create_report(self):
        self.click('finish:' + token('1'))
        nonce = self.ui.dialog(20)['nonce']
        self.message('/cancel')
        self.click('submit:' + nonce)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM completions').fetchone()[0], 0)

    def test_report_rejects_task_changed_during_attachment_collection(self):
        self.click('finish:' + token('1'))
        nonce = self.ui.dialog(20)['nonce']
        self.sheets.tasks[0][1]['Дедлайн'] = '12.10.2099 18:00'
        self.click('submit:' + nonce)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM completions').fetchone()[0], 0)

    def test_overview_review_and_request_filters(self):
        self.sheets.tasks += [(3, task(**{'Номер задачи': 'late', 'Дедлайн': '01.01.2000 18:00'})),
                              (4, task(**{'Номер задачи': 'none', 'Дедлайн': ''}))]
        CompletionFlow(self.store, self.tg, self.sheets, {10}).report(20, '1')
        self.message(OVERVIEW, uid=10)
        self.assertIn('Открытых: 3', self.tg.messages[-1][1])
        self.assertIn('Без срока: 1', self.tg.messages[-1][1])
        self.message(REVIEW, uid=10)
        self.assertIn('№1', self.tg.messages[-1][1])
        self.assertNotIn('№late', self.tg.messages[-1][1])

    def test_deadline_request_approval_changes_deadline_not_status_and_notifies(self):
        self.sent()
        request = self.request()
        self.assertEqual(self.sheets.tasks[0][1]['Дедлайн'], '06.10.2026 15:00')
        self.click('move_yes:' + request['id'], uid=10)
        self.assertEqual(self.sheets.tasks[0][1]['Дедлайн'], '10.10.2099 18:00')
        self.assertEqual(self.sheets.tasks[0][1]['Статус задачи'], 'Новая')
        self.assertTrue(any('Поручение №1 изменено' in text for uid, text in self.tg.messages if uid == 20))
        writes = len(self.sheets.writes)
        self.click('move_yes:' + request['id'], uid=10)
        self.assertEqual(writes, len(self.sheets.writes))

    def test_request_rejection_keeps_old_deadline_and_notifies(self):
        request = self.request()
        self.click('move_no:' + request['id'], uid=10)
        self.assertEqual(self.sheets.tasks[0][1]['Дедлайн'], '06.10.2026 15:00')
        self.assertTrue(any('отклонён' in text for uid, text in self.tg.messages if uid == 20))

    def test_request_cannot_be_approved_by_employee_or_after_reassignment(self):
        request = self.request()
        self.click('move_yes:' + request['id'])
        self.assertEqual(self.sheets.writes, [])
        self.sheets.tasks[0][1]['ID сотрудника'] = 'EMP002'
        self.click('move_yes:' + request['id'], uid=10)
        self.assertEqual(self.sheets.writes, [])

    def test_request_approval_write_failure_survives_restart(self):
        self.sent()
        request = self.request()
        self.sheets.fail_write = True
        self.click('move_yes:' + request['id'], uid=10)
        self.store.db.close()
        self.store = Store(self.path)
        self.ui = Workflow(self.store, self.tg, self.sheets, {10})
        self.sheets.fail_write = False
        self.ui.cycle()
        self.assertEqual(self.sheets.tasks[0][1]['Дедлайн'], '10.10.2099 18:00')
        self.assertEqual(self.store.db.execute('SELECT state FROM deadline_requests').fetchone()[0], 'approved')

    def test_admin_edit_previews_then_writes_and_notifies_once(self):
        self.sent()
        data = self.edit('text', 'Подготовить новый вариант договора')
        self.assertEqual(self.sheets.tasks[0][1]['Задача'], 'Подготовить договор')
        self.click('save:' + data['nonce'], uid=10)
        self.assertEqual(self.sheets.tasks[0][1]['Задача'], 'Подготовить новый вариант договора')
        self.ui.cycle()
        notices = [text for uid, text in self.tg.messages if uid == 20 and 'Поручение №1 изменено' in text]
        self.assertEqual(len(notices), 1)
        self.click('save:' + data['nonce'], uid=10)
        self.assertEqual(len([w for w in self.sheets.writes if w[0] == 'modify']), 1)

    def test_edit_conflict_does_not_overwrite_external_change(self):
        data = self.edit('text', 'Новая формулировка')
        self.sheets.tasks[0][1]['Задача'] = 'Изменено в таблице'
        self.click('save:' + data['nonce'], uid=10)
        self.assertEqual(self.sheets.tasks[0][1]['Задача'], 'Изменено в таблице')
        self.assertFalse(any(w[0] == 'modify' for w in self.sheets.writes))

    def test_edit_cannot_race_completion_report(self):
        data = self.edit('text', 'Новая формулировка')
        CompletionFlow(self.store, self.tg, self.sheets, {10}).report(20, '1')
        self.click('save:' + data['nonce'], uid=10)
        self.assertEqual(self.sheets.tasks[0][1]['Задача'], 'Подготовить договор')

    def test_reassignment_notifies_both_and_removes_old_access(self):
        self.sent()
        self.sheets.people['EMP002'] = {'ФИО': 'Петров Алексей', 'Активен': 'Да'}
        self.store.bind(self.store.invite('EMP002'), 30)
        self.click('edit:' + token('1'), uid=10)
        data = self.ui.dialog(10)
        self.click('field:employee:' + data['nonce'], uid=10)
        self.click('assignee:' + token('EMP002') + ':' + data['nonce'], uid=10)
        self.click('save:' + data['nonce'], uid=10)
        self.assertEqual(self.sheets.tasks[0][1]['ID сотрудника'], 'EMP002')
        self.assertTrue(any('передана другому сотруднику' in text for uid, text in self.tg.messages if uid == 20))
        self.assertTrue(any('Поручение №1 изменено' in text for uid, text in self.tg.messages if uid == 30))
        self.click('card:' + token('1'))
        self.assertIn('своими', self.tg.messages[-1][1])

    def test_external_edit_notifies_once_and_resets_acceptance(self):
        self.sent()
        self.ui.accept(20, token('1'))
        self.sheets.tasks[0][1]['Дедлайн'] = '11.10.2099 18:00'
        self.ui.cycle()
        self.ui.cycle()
        notices = [text for uid, text in self.tg.messages if uid == 20 and 'Поручение №1 изменено' in text]
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM task_acceptances').fetchone()[0], 0)

    def test_deploy_seeds_existing_tasks_without_change_notifications(self):
        self.store.enqueue('1', {'employee': 'EMP001'})
        self.store.set('1', state='sent')
        self.ui.cycle()
        self.assertFalse(any('изменено' in text for _, text in self.tg.messages))

    def test_reminders_thresholds_and_daily_digest_deduplicated(self):
        now = datetime(2099, 10, 3, 10, 0, tzinfo=MOSCOW)
        self.sheets.tasks[0][1]['Дедлайн'] = '03.10.2099 12:00'
        self.ui.reminders(*self.sheets.snapshot(), now=now)
        self.ui.flush()
        self.ui.reminders(*self.sheets.snapshot(), now=now)
        self.ui.flush()
        self.assertEqual(len(self.tg.messages), 1)
        self.ui.reminders(*self.sheets.snapshot(), now=now.replace(hour=11, minute=30))
        self.ui.flush()
        self.assertEqual(len(self.tg.messages), 2)
        self.ui.reminders(*self.sheets.snapshot(), now=now.replace(hour=13))
        self.ui.flush()
        self.ui.reminders(*self.sheets.snapshot(), now=now.replace(hour=14))
        self.ui.flush()
        self.assertEqual(len([text for uid, text in self.tg.messages if uid == 10]), 1)
        self.assertEqual(len([text for uid, text in self.tg.messages if uid == 20]), 3)

    def test_pending_report_suppresses_employee_reminders_and_stale_queue(self):
        now = datetime(2099, 10, 3, 10, 0, tzinfo=MOSCOW)
        self.sheets.tasks[0][1]['Дедлайн'] = '03.10.2099 12:00'
        self.ui.reminders(*self.sheets.snapshot(), now=now)
        CompletionFlow(self.store, self.tg, self.sheets, {10}).report(20, '1')
        self.tg.messages.clear()
        self.ui.flush()
        self.assertEqual(self.tg.messages, [])
        self.assertEqual(self.store.db.execute('SELECT state FROM ui_outbox').fetchone()[0], 'obsolete')

    def test_changed_or_closed_task_cancels_queued_reminder(self):
        self.sheets.tasks[0][1]['Дедлайн'] = '03.10.2099 12:00'
        self.ui.reminders(*self.sheets.snapshot(), now=datetime(2099, 10, 3, 10, tzinfo=MOSCOW))
        self.sheets.tasks[0][1]['Статус задачи'] = 'Выполнена'
        self.ui.flush()
        self.assertEqual(self.tg.messages, [])

    def test_reminder_configuration_and_disable(self):
        self.click('remind_hours', uid=10)
        self.message('48, 2', uid=10)
        self.assertEqual(self.ui.reminder_hours(), [48, 2])
        self.click('remind_toggle', uid=10)
        self.tg.messages.clear()
        self.sheets.tasks[0][1]['Дедлайн'] = '01.01.2000 18:00'
        self.ui.reminders(*self.sheets.snapshot())
        self.ui.flush()
        self.assertEqual(self.tg.messages, [])

    def test_outbox_retry_keeps_progress_and_restart_state(self):
        self.ui.queue('long', 20, 'Я' * 6000, guard={'employee': 'EMP001'})
        self.tg.errors = [None, APIError('retry', 'slow', 1)]
        self.ui.flush()
        self.assertEqual(len(self.tg.messages), 1)
        with self.store.db:
            self.store.db.execute('UPDATE ui_outbox SET retry_at=0')
        self.store.db.close()
        self.store = Store(self.path)
        self.ui = Workflow(self.store, self.tg, self.sheets, {10})
        self.ui.flush()
        self.assertEqual(len(self.tg.messages), 2)

    def test_date_parser_and_inputs_are_moscow(self):
        self.assertEqual(deadline('03.10.2026').hour, 23)
        self.assertIsNone(deadline('в пятницу'))
        self.assertEqual(input_deadline('03.10.2026 18:30').utcoffset(), timedelta(hours=3))
        with self.assertRaises(ValueError):
            input_deadline('завтра')

    def test_guarded_google_edit_writes_selected_cells_only_and_verifies(self):
        sheets = Sheets.__new__(Sheets)
        sheets.task_sheet = 'Задачи'
        original = task()
        modified = dict(original, **{'Задача': 'Новая формулировка'})
        with patch.object(sheets, 'request', side_effect=[{'values': [list(original.values())[:8]]}, {}, {'values': [list(modified.values())[:8]]}]) as request:
            sheets.modify(2, original, {'Задача': 'Новая формулировка'})
            body = request.call_args_list[1].kwargs['json']
            self.assertEqual(body['data'], [{'range': "'Задачи'!C2", 'values': [['Новая формулировка']]}])
        with patch.object(sheets, 'request', return_value={'values': [list(task(**{'Номер задачи': '2'}).values())[:8]]}) as request:
            with self.assertRaises(ValueError):
                sheets.modify(2, original, {'Задача': 'Новая формулировка'})
            self.assertEqual(request.call_count, 1)

    def test_old_database_migration_preserves_reports_and_bindings(self):
        legacy_path = str(Path(self.tmp.name) / 'old.db')
        db = sqlite3.connect(legacy_path)
        db.execute("CREATE TABLE completions(id TEXT PRIMARY KEY, task TEXT, employee TEXT, chat INTEGER, payload TEXT, state TEXT, notices TEXT, result_progress INTEGER)")
        db.execute("INSERT INTO completions VALUES('old','1','EMP001',20,'{}','pending','{}',0)")
        db.commit()
        db.close()
        upgraded = Store(legacy_path)
        try:
            row = upgraded.db.execute('SELECT * FROM completions').fetchone()
            self.assertEqual(row['id'], 'old')
            self.assertEqual(row['evidence'], '[]')
            self.assertEqual(row['reason'], '')
        finally:
            upgraded.db.close()


if __name__ == '__main__':
    unittest.main()
