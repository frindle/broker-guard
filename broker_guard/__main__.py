"""``python -m broker_guard`` -> the service entrypoint."""
from broker_guard.service import main

if __name__ == "__main__":
    raise SystemExit(main())
