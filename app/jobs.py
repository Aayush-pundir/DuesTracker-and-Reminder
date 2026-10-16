"""`python -m app.jobs` - run the daily automation once (for cron / GitHub Actions / Task Scheduler)."""
from . import db, reminders
c = db.connect(); db.init(c)
print(reminders.run(c))
