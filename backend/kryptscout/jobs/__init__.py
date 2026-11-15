"""Scheduled jobs for the prediction-history store.

Three entry points, all run as modules from the ``backend`` directory:

    python -m kryptscout.jobs.capture [--sports nba,mlb,...] [--no-odds]
        Pull today's slates + model predictions (+ market odds when
        ODDS_API_KEY is set) and record them into the store. Run this a few
        times a day — every capture adds an odds snapshot per book, which is
        what powers line movement and CLV. Works without an odds key
        (predictions still recorded).

    python -m kryptscout.jobs.settle
        Fill final scores for recently started games from ESPN, then print
        the accuracy summary. Run once daily, e.g. mid-morning UTC when the
        previous night's games are all final.

    python -m kryptscout.jobs.refresh [--sports nba,mlb,...] [--force]
        Run every registered engine's refresh_data() sequentially — pull the
        latest results and update ratings — printing timing and a
        state-file summary per sport. ``--force`` deletes any model pickle
        named after the sport first (no engine writes one today). Run
        once daily, after settle, so the day's boards predict from fresh
        state. The Electron scheduler's daily refresh job hits the equivalent
        POST /api/jobs/refresh instead of shelling out to this CLI.

Scheduling on Windows (Task Scheduler)
--------------------------------------
Create the tasks from an elevated prompt (adjust the paths and cadence):

    schtasks /Create /TN "KryptScout Capture" /SC HOURLY /MO 4 ^
        /TR "cmd /c cd /d C:\\Users\\maxwe\\Desktop\\kryptware\\Krypt Goaly\\backend && python -m kryptscout.jobs.capture"

    schtasks /Create /TN "KryptScout Settle" /SC DAILY /ST 11:00 ^
        /TR "cmd /c cd /d C:\\Users\\maxwe\\Desktop\\kryptware\\Krypt Goaly\\backend && python -m kryptscout.jobs.settle"

    schtasks /Create /TN "KryptScout Refresh" /SC DAILY /ST 11:30 ^
        /TR "cmd /c cd /d C:\\Users\\maxwe\\Desktop\\kryptware\\Krypt Goaly\\backend && python -m kryptscout.jobs.refresh"

(Or use the Task Scheduler GUI: action = "Start a program", program =
``cmd``, arguments = the ``/c cd /d ... && python -m ...`` part.)

Scheduling on cron (Linux/macOS/WSL)
------------------------------------
    # capture every 4 hours, settle daily at 11:00 UTC, refresh at 11:30 UTC
    0 */4 * * *  cd /path/to/backend && python -m kryptscout.jobs.capture >> /var/log/kryptscout-capture.log 2>&1
    0 11 * * *   cd /path/to/backend && python -m kryptscout.jobs.settle  >> /var/log/kryptscout-settle.log 2>&1
    30 11 * * *  cd /path/to/backend && python -m kryptscout.jobs.refresh >> /var/log/kryptscout-refresh.log 2>&1

Notes
-----
- The Odds API free tier is 500 requests/month; each capture spends roughly
  one request per sport key (soccer/tennis span several). Use ``--sports``
  or ``--no-odds`` to stay inside the budget.
- Both jobs are idempotent: re-running capture within an hour updates the
  same prediction rows instead of duplicating them, and settle only touches
  games that are still pending.
"""
