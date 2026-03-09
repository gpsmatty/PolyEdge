.PHONY: install dev test scan hunt edges autopilot dashboard setup initdb

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	python -m pytest tests/ -v

scan:
	polyedge scan

hunt:
	polyedge hunt

edges:
	polyedge edges

autopilot:
	polyedge autopilot --mode autopilot

copilot:
	polyedge autopilot --mode copilot

signals:
	polyedge autopilot --mode signals

dashboard:
	polyedge dashboard

setup:
	polyedge setup

initdb:
	polyedge initdb
