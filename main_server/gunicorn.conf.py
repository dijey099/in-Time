"""Gunicorn settings of the systemd service: address and number of processes from main.conf [server].

    .venv/bin/gunicorn -c gunicorn.conf.py main:app
"""
from config import Config

bind = Config.SERVER_BIND
workers = Config.SERVER_WORKERS
threads = 4
timeout = 120
accesslog = "-"
