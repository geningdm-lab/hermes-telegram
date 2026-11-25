"""``python -m acceptance``: the live acceptance run on vtadevbot (acceptance/README.md).

    rows                      list the rows
    login                     log in to the owner's account (asks for the phone, the code and the 2FA password)
    run [ROW ...]             start the acceptance gateway, run the rows (all by default), write the report
    logout                    end the session on Telegram and delete it
"""

import argparse
import asyncio
import datetime
import sys
import time
import traceback

from .home import STATE, Gateway, prepare
from .rows import ROWS, Result, Run


async def _ask(question: str) -> bool:
    answer = await asyncio.to_thread(input, f"\n{question}\n  [y/n] > ")
    return answer.strip().lower().startswith("y")


async def _pause(instruction: str) -> None:
    await asyncio.to_thread(input, f"\n{instruction}\n  [Enter] > ")


def _report(results: list[Result], started: float, args: argparse.Namespace) -> str:
    lines = [f"# Acceptance run on {args.bot}, {datetime.datetime.fromtimestamp(started):%Y-%m-%d %H:%M}", "",
             f"Model: {args.provider} {args.model}. Rows run: {', '.join(r.row for r in results)}.", "",
             "| Row | Title | Verdict |", "|---|---|---|"]
    lines += [f"| {r.row} | {r.title} | {'pass' if r.passed else 'FAIL'} |" for r in results]
    for r in results:
        lines += ["", f"## Row {r.row}: {r.title}", ""]
        lines += [f"- {'ok' if ok else 'FAIL'}: {what}" + (f" ({detail})" if detail and not ok else "")
                  for what, ok, detail in r.checks]
        lines += [f"- owner {'yes' if ok else 'NO'}: {question}" for question, ok in r.observed]
        if r.error:
            lines += ["", "```text", r.error.strip(), "```"]
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> int:
    from . import samples
    from .live import Chat, client

    keys = args.rows or list(ROWS)
    unknown = [k for k in keys if k not in ROWS]
    if unknown:
        raise SystemExit(f"no such row: {', '.join(unknown)}")
    tg = client()
    await tg.start()
    me = await tg.get_me()
    prepare(me.id, provider=args.provider, model=args.model)
    files = samples.generate()
    gateway = Gateway(me.id)
    gateway.start()
    started, results = time.time(), []
    try:
        chat = await Chat.open(tg, args.bot)
        run = Run(chat=chat, owner_id=me.id, files=files, gateway=gateway, ask=_ask, pause=_pause)
        for key in keys:
            title, fn = ROWS[key]
            print(f"\n=== Row {key}: {title}")
            run.result = Result(key, title)
            try:
                await fn(run)
            except Exception:
                run.result.error = traceback.format_exc()
            results.append(run.result)
            print(f"--- Row {key}: {'pass' if run.result.passed else 'FAIL'}")
    finally:
        gateway.stop()
        await tg.disconnect()
        if results:
            out = STATE / "reports" / f"acceptance-{datetime.datetime.fromtimestamp(started):%Y%m%d-%H%M%S}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_report(results, started, args), encoding="utf-8")
            print(f"\nReport: {out}")
    return 0 if all(r.passed for r in results) else 1


async def _login() -> None:
    from .live import client

    tg = client()
    await tg.start()
    me = await tg.get_me()
    print(f"Logged in as {me.first_name} (id {me.id}); the session is in {STATE}.")
    await tg.disconnect()


async def _logout() -> None:
    from .live import SESSION, client

    tg = client()
    await tg.connect()
    if await tg.is_user_authorized():
        await tg.log_out()
    await tg.disconnect()
    for path in STATE.glob(SESSION.name + ".session*"):
        path.unlink()
    print("Logged out; the session file is deleted.")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m acceptance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rows")
    sub.add_parser("login")
    sub.add_parser("logout")
    run = sub.add_parser("run")
    run.add_argument("rows", nargs="*")
    run.add_argument("--bot", default="vtadevbot")
    run.add_argument("--provider", default="anthropic")
    run.add_argument("--model", default="claude-sonnet-5-5")
    args = parser.parse_args()
    if args.command == "rows":
        for key, (title, _) in ROWS.items():
            print(f"{key:>4}  {title}")
        return 0
    if args.command == "login":
        asyncio.run(_login())
        return 0
    if args.command == "logout":
        asyncio.run(_logout())
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
