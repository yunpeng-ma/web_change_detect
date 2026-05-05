from __future__ import annotations

import os

import main


TEST_URL = (
    "https://www.random.org/integers/"
    "?num=1&min=1&max=1000000&col=1&base=10&format=plain&rnd=new"
)


def run_test() -> None:
    main.load_env()
    os.environ["URL"] = TEST_URL

    if main.STATE_FILE.exists():
        main.STATE_FILE.unlink()

    print("First check should save the initial state.")
    main.check_once(TEST_URL)

    print("Second check should detect a change and send a notification.")
    main.check_once(TEST_URL)


if __name__ == "__main__":
    run_test()
