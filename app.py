"""Convenient Flask entry point; the application implementation lives in backend.py."""
from backend import app, init_db

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
