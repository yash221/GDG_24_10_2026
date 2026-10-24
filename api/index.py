# Vercel entry point: every request is routed here (see vercel.json)
from app.main import create_app

app = create_app()
