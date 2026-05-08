from __future__ import annotations

import subprocess


class PassStoreError(RuntimeError):
    pass


class PassStore:
    def __init__(self, *, executable: str = "pass") -> None:
        self._executable = executable

    def show(self, path: str) -> str:
        result = subprocess.run(
            [self._executable, "show", path],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PassStoreError(f"pass show failed for {path!r}")
        return result.stdout.rstrip("\n")

    def insert_multiline(self, path: str, value: str) -> None:
        result = subprocess.run(
            [self._executable, "insert", "-m", "-f", path],
            input=value if value.endswith("\n") else value + "\n",
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PassStoreError(f"pass insert failed for {path!r}")
