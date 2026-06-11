# Local dev entry point — Vercel uses api/index.py directly
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api.index import app

if __name__ == '__main__':
    app.run(debug=True, port=5001)
