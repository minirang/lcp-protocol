.PHONY: install test lint type fmt bench build clean

install:
	python -m pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .
	ruff format --check .

type:
	mypy

fmt:
	ruff format .
	ruff check --fix .

bench:
	python -m benchmarks.bench --sizes 100,1024 --concurrency 1,16 --duration 2

build:
	python -m build

clean:
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache .hypothesis .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
