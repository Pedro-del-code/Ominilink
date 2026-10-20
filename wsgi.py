"""Ponto de entrada WSGI (gunicorn): `gunicorn wsgi:app`."""
from app.api import create_app

app = create_app()
