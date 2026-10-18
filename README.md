# Wick Hunter KCEX — Research Prototype

Read [README_FA.md](README_FA.md) for Persian setup instructions, limitations, and security notes.

**Unverified KCEX futures candle endpoint.** The scanner fails closed on unsupported or stale feeds. Offline test: `python scanner.py --demo && python -m unittest discover -s tests -v`. No order placement, trade keys, or trading actions.
