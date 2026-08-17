#!/bin/bash
URL="https://hc-ping.com/26a08a90-9042-45e5-bab5-3fed344dacb7"
curl -fsS -m 10 --retry 5 -o /dev/null "$URL/start"
cd /apps/local/cetlocron/data/apps/demoStudent
~/.local/bin/uv run demo-student run --semesters 1 --recheck
EXIT=$?
curl -fsS -m 10 --retry 5 -o /dev/null "$URL/$EXIT"