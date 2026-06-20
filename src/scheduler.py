from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from src.config_manager import load_settings

_scheduler = BackgroundScheduler()


def init_scheduler(app):
    settings = load_settings()
    interval = int(settings['email'].get('polling_interval_minutes', 5))

    _scheduler.add_job(
        func=lambda: _poll(app),
        trigger=IntervalTrigger(minutes=interval),
        id='email_poll',
        replace_existing=True,
    )
    if not _scheduler.running:
        _scheduler.start()
    return _scheduler


def _poll(app):
    with app.app_context():
        from src.invoice_processor import process_new_emails
        try:
            process_new_emails()
        except Exception as e:
            app.logger.error(f"Scheduled email poll error: {e}")


def shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
