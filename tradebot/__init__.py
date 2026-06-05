"""Capital-preservation crypto trading bot.

The same strategy / risk / portfolio code runs identically in backtest, paper,
and live. Only the data feed and the execution backend are swapped.
"""

__version__ = "0.1.0"

# ccxt probes git for its version on first import and leaks a harmless
# "fatal: bad revision 'HEAD'" to stderr when launched inside (or under) a git
# repo with an unborn HEAD — e.g. a parent folder that's an empty git repo.
# Warm the import here once with the OS-level stderr silenced so the console
# stays clean; later `import ccxt` calls hit the module cache (no git, no noise).
# Real import errors still propagate (stderr is restored in finally first).
import os as _os  # noqa: E402

with open(_os.devnull, "w") as _devnull:
    _saved_fd = _os.dup(2)
    _os.dup2(_devnull.fileno(), 2)
    try:
        import ccxt as _ccxt  # noqa: F401,E402
    finally:
        _os.dup2(_saved_fd, 2)
        _os.close(_saved_fd)
