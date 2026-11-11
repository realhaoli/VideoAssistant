"""A pipeline yields work to the appropriate bounded stage queue."""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Step:
    queue: str
    run: Callable
    after: int | None = None


@dataclass(frozen=True)
class ParallelSteps:
    steps: tuple[Step, ...]

    def submit(self, submit, check=lambda: None):
        futures = tuple(Future() for _ in self.steps)

        def enqueue(step, future):
            if future.cancelled():
                return

            def run():
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    check()
                    result = step.run()
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)

            try:
                scheduled = submit(step.queue, run)
            except RuntimeError as exc:
                if future.set_running_or_notify_cancel():
                    future.set_exception(exc)
            else:
                scheduled.add_done_callback(lambda done: future.cancel() if done.cancelled() else None)

        for step, future in zip(self.steps, futures):
            if step.after is None:
                enqueue(step, future)
            else:
                futures[step.after].add_done_callback(
                    lambda done, item=step, result=future: result.cancel() if done.cancelled() else enqueue(item, result))
        return futures

    def run(self):
        with ThreadPoolExecutor(max_workers=len(self.steps)) as pool:
            futures = self.submit(lambda _, run: pool.submit(run))
            return [future.result() for future in futures]


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
