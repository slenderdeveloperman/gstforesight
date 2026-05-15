# gst-foresight 🔮

> An open-source regulatory foresight engine for Indian GST — predicts incoming CBIC circulars, rate changes, and rule amendments before they're announced.

**Live project. Predictions update automatically. Data sources added as they prove useful.**

---

## What this does

GST rules change constantly. Circulars drop with little warning. Rate changes get announced at GST council meetings. CAs and businesses are always reactive.

This project flips that. It watches the public corpus of regulatory signals — council minutes, budget speeches, judicial rulings, industry submissions — and surfaces probability-weighted predictions of what's likely to change next and when.

It doesn't scrape the GST portal. It doesn't require a GSP license. It reads the signals that precede rule changes.

---

## Current predictions

See [`data/predictions/latest.json`](data/predictions/latest.json) for live predictions, or the [dashboard](#dashboard).

---

## Data sources (active)

| Source | Signal type | Update frequency |
|--------|------------|-----------------|
| CBIC circulars (cbic.gov.in) | Historical pattern — what topics get clarified repeatedly | Weekly |
| GST Council meeting minutes | Vote patterns, deferred agenda items | Per meeting (~6/year) |
| AAR rulings corpus | Judicial pressure by topic tag | Weekly |
| Budget speech corpus (2017–present) | Language pattern before council action | Annual |
| ICAI pre-budget memoranda | Industry ask frequency | Annual |

## Data sources (planned — PRs welcome)

- [ ] State election calendar × council vote pattern model
- [ ] High court GST division bench orders
- [ ] FICCI/CII GST committee submissions
- [ ] Ministry of Finance press releases
- [ ] Rajya Sabha / Lok Sabha GST question corpus
- [ ] Informal: ICAI newsletter signals

---

## How predictions work

Each prediction has:
- **Topic**: the GST provision or rule area likely to change
- **Change type**: clarification / rate change / new rule / amendment
- **Probability**: 0–100%, updated as new signals arrive
- **Horizon**: expected timeframe (next council meeting / next budget / 2–3 quarters)
- **Signal strength**: which data sources are driving the prediction
- **Backtest accuracy**: how this signal type has performed historically

The model is intentionally simple and explainable. No black boxes. Every prediction links to the raw signals that generated it.

---

## Adding a new data source

1. Add a scraper in `scrapers/` following the `BaseScraper` interface
2. Add a processor in `processors/` that tags content with topic labels
3. Update `config/sources.yaml` with the new source metadata
4. Run `python -m gst_foresight.ingest --source your_source`
5. Open a PR with a note on why this source adds signal

The project is designed so you can add a new source in an afternoon without touching the prediction logic.

---

## Running locally

```bash
git clone https://github.com/slenderdeveloperman/gstforesight
cd gst-foresight
pip install -r requirements.txt

# Ingest all sources
python -m gst_foresight.ingest --all

# Run predictions
python -m gst_foresight.predict

# Serve dashboard
python -m gst_foresight.dashboard
```

---

## Contributing

This is a living project. Contributions welcome:
- New data sources (scrapers + processors)
- Better topic taxonomy
- Improved prediction models
- Backtest improvements
- Dashboard features

Open an issue before building something large. For small things, just PR.

---

## License

MIT. Use it, fork it, build on it.

---

*Built because GST compliance shouldn't always be reactive.*
