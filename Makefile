.PHONY: test lint run dashboard install

install:
	pip install -r requirements.txt
	pip install ruff pytest

test:
	ASTROTICK_SKIP_DOTENV=1 python -m pytest tests/ -q

lint:
	ruff check .

run:
	python bot.py

dashboard:
	python dashboard.py
