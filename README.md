# Trade

An options trading server with one web page, for more than one market:

- **US** options through Tiger Brokers
- **India** (NIFTY) options through OpenAlgo / Angel One

Start it with `python -m backend.main`, then open http://127.0.0.1:8000/ui.

- How to set up and run it: [RUN.txt](RUN.txt)
- How the code is organised: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Why things are the way they are: [docs/HANDOVER.md](docs/HANDOVER.md)

Settings live in `config/` (one file per market). Credentials, logs and saved
state are gitignored and never committed.
