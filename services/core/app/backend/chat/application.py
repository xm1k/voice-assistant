"""One container owns the domain API and its four background processes."""

import asyncio
import signal
import sys


async def supervise(commands):
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    processes = {}
    tasks = []
    try:
        for name, command in commands.items():
            processes[name] = await asyncio.create_subprocess_exec(*command)
            print(f"Started {name}: pid={processes[name].pid}", flush=True)
        tasks = [asyncio.create_task(process.wait()) for process in processes.values()]
        stop = asyncio.create_task(stopping.wait())
        tasks.append(stop)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if stop in done:
            return 0
        for name, process in processes.items():
            if process.returncode is not None:
                print(f"{name} exited unexpectedly: status={process.returncode}", file=sys.stderr, flush=True)
        return 1
    finally:
        for process in processes.values():
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(asyncio.gather(*(p.wait() for p in processes.values())), timeout=20)
        except asyncio.TimeoutError:
            for process in processes.values():
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            await asyncio.gather(*(p.wait() for p in processes.values()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main():
    python = sys.executable
    return asyncio.run(
        supervise(
            {
                "api": [
                    python,
                    "-m",
                    "uvicorn",
                    "backend.chat.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                    "--no-access-log",  # OAuth callback contains a short-lived authorization code.
                ],
                "publisher": [python, "-m", "backend.chat.broker", "publisher"],
                "results": [python, "-m", "backend.chat.broker", "results"],
                "reminders": [python, "-m", "backend.chat.broker", "reminders"],
                "index": [python, "-m", "backend.workers.index"],
            }
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
