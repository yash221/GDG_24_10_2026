# Vercel entry point (FastAPI zero-config looks for `app` in index.py)
from app.main import create_app

app = create_app()
