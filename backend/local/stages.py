"""A pipeline yields work to the appropriate bounded stage queue."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Step:
    queue: str
    run: Callable


def drive(steps):
    """Run the same steps inline for synchronous callers."""
    result = None
    error = None
    try:
        while True:
            step = steps.throw(error) if error is not None else steps.send(result)
            error = None
            try:
                result = step.run()
            except Exception as exc:
                error = exc
    except StopIteration as done:
        return done.value
