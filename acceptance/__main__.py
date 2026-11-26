"""``python -m acceptance``: the live acceptance run on vtadevbot (acceptance/README.md).

    rows                      list the rows
    playbook                  print the agent's playbook (the codes the owner sends and their steps)
    run [ROW ...]             start the proxy and the acceptance gateway, run the rows (all by default) with the
                              owner sending the codes from iOS, write the report
"""

import argparse
import asyncio
import datetime
import os
import sys
import time
import traceback

import aiohttp

from .home import STATE, Gateway, prepare
from .recorder import UPSTREAM, Recorder
from .rows import ROWS, Result, Run, playbook

OWNER_FILE = STATE / "owner-id"


async def _ask(question: str) -> bool:
    answer = await asyncio.to_thread(input, f"\n{question}\n  [y/n] > ")
    return answer.strip().lower().startswith(("y", "д"))


async def _pause(instruction: str) -> None:
    await asyncio.to_thread(input, f"\n{instruction}\n  [Enter] > ")


def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Run under `op run --env-file acceptance/op.env`: the bot token comes from 1Password "
                         "(acceptance/README.md).")
    return token


async def find_owner(token: str) -> int:
    """The owner's Telegram user id: ``TELEGRAM_OWNER_ID``, else the one an earlier run saved, else the sender of
    the next private message to the bot, once the owner confirms it is them. Polling the bot here also shows
    whether another process (the host's gateway) still polls it."""
    if os.environ.get("TELEGRAM_OWNER_ID"):
        return int(os.environ["TELEGRAM_OWNER_ID"])
    if OWNER_FILE.exists():
        return int(OWNER_FILE.read_text().strip())
    print("\nOn iOS, send the bot any message from your account (it tells this run who the owner is).")
    base, offset = f"{UPSTREAM}/bot{token}", 0
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=60)) as session:
        while True:
            async with session.post(f"{base}/getUpdates", data={"timeout": "30", "offset": str(offset)}) as reply:
                body = await reply.json()
            if not body.get("ok"):
                hint = " Stop the host's vtadevbot gateway first." if reply.status == 409 else ""
                raise SystemExit(f"Telegram refused getUpdates: {body.get('description')}.{hint}")
            for update in body["result"]:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                who = message.get("from") or {}
                if (message.get("chat") or {}).get("type") != "private" or not who:
                    continue
                name = " ".join(filter(None, (who.get("first_name"), who.get("last_name"))))
                if await _ask(f"Is this you: {name} @{who.get('username', '')} (id {who['id']})?"):
                    # Confirm it, so the gateway does not answer it.
                    async with session.post(f"{base}/getUpdates", data={"offset": str(offset), "timeout": "0"}):
                        pass
                    STATE.mkdir(parents=True, exist_ok=True)
                    OWNER_FILE.write_text(str(who["id"]))
                    return int(who["id"])


def _report(results: list[Result], started: float, args: argparse.Namespace, record: str) -> str:
    lines = [f"# Acceptance run on {args.bot}, {datetime.datetime.fromtimestamp(started):%Y-%m-%d %H:%M}", "",
             f"Model: {args.provider} {args.model}. Rows run: {', '.join(r.row for r in results)}. "
             f"Record of the bot's calls: `{record}`.", "",
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

    keys = args.rows or list(ROWS)
    unknown = [k for k in keys if k not in ROWS]
    if unknown:
        raise SystemExit(f"no such row: {', '.join(unknown)}")
    token = _token()
    owner = await find_owner(token)
    files = samples.generate()
    started, results = time.time(), []
    record = STATE / "records" / f"record-{datetime.datetime.fromtimestamp(started):%Y%m%d-%H%M%S}.jsonl"
    recorder = Recorder(record, token)
    api = await recorder.start()
    prepare(owner, provider=args.provider, model=args.model, api=api, playbook=playbook(files))
    gateway = Gateway(owner)
    try:
        print("\nStarting the acceptance gateway...")
        await asyncio.to_thread(gateway.start)
        print(f"Open the chat with @{args.bot} on iOS. Each row says what to send or tap there, and asks here.")
        run = Run(rec=recorder, owner_id=owner, files=files, gateway=gateway, ask=_ask, pause=_pause,
                  sha={name: samples.sha256(path.read_bytes()) for name, path in files.items()})
        for key in keys:
            title, fn = ROWS[key]
            print(f"\n=== Row {key}: {title}")
            run.result = Result(key, title)
            try:
                await fn(run)
            except Exception:
                run.result.error = traceback.format_exc()
                print(run.result.error)
            results.append(run.result)
            print(f"--- Row {key}: {'pass' if run.result.passed else 'FAIL'}")
    finally:
        await asyncio.to_thread(gateway.stop)
        await recorder.stop()
        if results:
            out = STATE / "reports" / f"acceptance-{datetime.datetime.fromtimestamp(started):%Y%m%d-%H%M%S}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_report(results, started, args, str(record)), encoding="utf-8")
            print(f"\nReport: {out}")
    return 0 if all(r.passed for r in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m acceptance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rows")
    sub.add_parser("playbook")
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
    if args.command == "playbook":
        from .samples import SAMPLES
        names = ["document", "photo", "video", "audio", "voice", "big", "empty", "stop",
                 *(f"photo-{n:02d}" for n in range(1, 13))]
        print(playbook({n: SAMPLES / f"<{n}>" for n in names}))
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
