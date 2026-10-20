# Trading Copilot — Starter

A fresh, mobile-first starter for an integrated AI trading and investing copilot. The first milestone is deliberately read-only: no broker credentials, live market feed, or order placement is configured.

## Prerequisites
- Python 3.11 or newer
- Node.js 20 or newer and npm

## 1. Start the FastAPI backend

```bash
cd backend
python -m venv .venv
```

Activate the environment:
- macOS/Linux: `source .venv/bin/activate`
- Windows PowerShell: `.venv\\Scripts\\Activate.ps1`

Then run:

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

Check `http://127.0.0.1:8000/api/v1/health` and open `http://127.0.0.1:8000/docs`.

## 2. Start the frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open the local URL printed by Vite (usually `http://localhost:5173`).

## Current scope
- Responsive React + TypeScript dashboard shell
- FastAPI application with CORS configuration
- Health endpoint and interactive API documentation
- Four module placeholders

## Next milestones
1. Add PostgreSQL, SQLAlchemy models and Alembic migrations.
2. Add authentication and user-scoped portfolios.
3. Build trade journal and transparent P&L calculations.
4. Add market-data adapters, starting with delayed/historical data.
5. Add research, backtesting, monitoring and AI tool orchestration.
6. Add paper trading before considering broker order APIs.

Never put API keys in frontend code or commit `.env` files. Market data licences, exchange permissions and broker rules must be checked before production use.
