# Shortcuts. Run `make` (or `make help`) to list them.
PY := .venv/bin/python

.PHONY: help setup run-dash stop-dash run run-captions stop-run watch dry-run status discover rebuild test migrate-status \
        schedule-install schedule-status schedule-uninstall \
        annotate-setup annotate annotate-pilot annotate-dry recompute calibrate eval-gold \
        run-xg-dash stop-xg-dash test-annotate

help:  ## list the shortcuts
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-20s %s\n", $$1, $$2}'

setup:  ## create .venv and install requirements
	python3 -m venv .venv && $(PY) -m pip install -r requirements.txt

run-dash:  ## start the dashboard at http://127.0.0.1:5057 (auto-reloads on code changes)
	.venv/bin/flask --app dashboard/app.py run --debug --port 5057

stop-dash:  ## stop the dashboard
	-@pkill -f "dashboard/app.py" && echo "dashboard stopped" || echo "dashboard was not running"

run:  ## full pipeline run: discover, captions, MoneyPuck data, alignment
	$(PY) scripts/pipeline.py run --trigger manual

run-captions:  ## pipeline run without alignment (no video frames)
	$(PY) scripts/pipeline.py run --trigger manual --max-align 0

stop-run:  ## stop a pipeline run in progress (Ctrl-C: it's recorded as failed; frames read so far are kept)
	-@pkill -INT -f "scripts/pipeline.py" && echo "run stopping" || echo "no run in progress"

watch:  ## follow the latest run's log (Ctrl-C to stop watching; the run keeps going)
	@tail -n 30 -f "$$(ls -t data/logs/run_*.log | head -1)"

dry-run:  ## show what a run would do; no network
	$(PY) scripts/pipeline.py run --dry-run

status:  ## queue, YouTube cooldown and recent runs
	$(PY) scripts/pipeline.py status

discover:  ## find new replays on YouTube (official API only)
	$(PY) scripts/pipeline.py discover

rebuild:  ## rebuild the database from data/raw, offline
	XG_OFFLINE=1 $(PY) scripts/pipeline.py rebuild

test:  ## run the unit tests
	$(PY) -m unittest discover tests

migrate-status:  ## list database migrations and whether they're applied
	$(PY) scripts/db/migrate.py status

schedule-install:  ## install the weekly job (Mondays 03:00)
	$(PY) scripts/install_schedule.py install

schedule-status:  ## is the weekly job installed?
	$(PY) scripts/install_schedule.py status

schedule-uninstall:  ## remove the weekly job
	$(PY) scripts/install_schedule.py uninstall

# --- commentary -> xG (annotate/, Node) -------------------------------------
# Reads the same Postgres warehouse. The schema belongs to the Python migrations above;
# this side only reads and writes rows.
NPM := npm --prefix annotate

annotate-setup:  ## install the Node dependencies for annotate/
	$(NPM) install

annotate-pilot:  ## annotate 50 shots (free tier; check the output before a full run)
	$(NPM) run annotate -- --limit 50

annotate:  ## annotate every shot not done yet (resumable; stops when the day's quota runs out)
	$(NPM) run annotate

annotate-dry:  ## show the prompt for 2 shots; calls no model, writes nothing
	$(NPM) run annotate -- --dry-run --limit 2

recompute:  ## re-score stored features against the current coefficients; no model calls
	$(NPM) run annotate -- --recompute

calibrate:  ## normalise the adjusted xG and score it against real goals
	$(NPM) run calibrate

eval-gold:  ## score extraction against the hand-labelled gold set
	$(NPM) run eval-gold

run-xg-dash:  ## start the xG dashboard at http://127.0.0.1:5058
	$(NPM) run dash

stop-xg-dash:  ## stop the xG dashboard
	@# tsx runs it as "src/server.ts" from inside annotate/, so match on that, not the full path.
	-@pkill -f "tsx src/server.ts" && echo "xG dashboard stopped" || echo "xG dashboard was not running"

test-annotate:  ## run the annotator's unit tests
	$(NPM) test
