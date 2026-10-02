"""Entry point for hosts that start a Python file without CLI arguments."""
import sys
import bot

if __name__ == '__main__':
    if len(sys.argv) == 1:
        sys.argv.append('run')
    try:
        bot.main()
    except bot.APIError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit('Ошибка запуска. Проверьте зависимости, .env и доступ к сервисам.') from None
