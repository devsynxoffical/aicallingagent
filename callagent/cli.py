"""Command-line interface.

    callagent onboard script.md --name "Acme Q4"   # learn the pitch, answer its questions
    callagent rehearse "Acme Q4" --apply           # self-train against simulated prospects
    callagent leads import "Acme Q4" leads.xlsx    # load the sheet
    callagent serve                                # start webhooks + media server
    callagent campaign start "Acme Q4"             # dial
    callagent campaign status "Acme Q4"
    callagent campaign export "Acme Q4" results.csv
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .config import get_settings, resolve_api_token
from .db import Campaign, get_campaign_by_name, session_scope
from .playbook.schema import Playbook, QAPair

app = typer.Typer(help="Outbound AI calling agent that learns your pitch, dials your sheet and closes.", no_args_is_help=True)
leads_app = typer.Typer(help="Lead sheets.")
campaign_app = typer.Typer(help="Run and monitor campaigns (needs `callagent serve` running).")
playbook_app = typer.Typer(help="Inspect and edit playbooks.")
app.add_typer(leads_app, name="leads")
app.add_typer(campaign_app, name="campaign")
app.add_typer(playbook_app, name="playbook")

console = Console()


def _playbook_path(name: str) -> Path:
    settings = get_settings()
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower())
    return settings.data_dir / "playbooks" / f"{safe}.json"


def _save_campaign(name: str, playbook: Playbook, script_text: str) -> Campaign:
    path = _playbook_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(playbook.model_dump_json(indent=2))
    with session_scope() as s:
        c = get_campaign_by_name(s, name)
        if c is None:
            c = Campaign(name=name, status="draft")
            s.add(c)
        c.playbook_json = playbook.model_dump()
        c.script_text = script_text or c.script_text
        # Running/paused/done campaigns keep their state; drafts become ready once nothing blocks.
        if c.status in ("draft", "ready"):
            c.status = "ready" if playbook.is_ready else "draft"
        s.flush()
        s.refresh(c)
        return c


def _load_playbook(name: str) -> tuple[Playbook, str]:
    with session_scope() as s:
        c = get_campaign_by_name(s, name)
        if c is None:
            raise typer.BadParameter(f"campaign '{name}' not found. Run `callagent onboard` first.")
        return Playbook.model_validate(c.playbook_json), c.script_text


def _print_playbook_summary(pb: Playbook) -> None:
    console.print(Panel.fit(
        f"[bold]{pb.company_name}[/bold]\n{pb.offer_summary}\n\n"
        f"Goal: [cyan]{pb.call_goal}[/cyan] - {pb.call_goal_details}\n"
        f"Agent: {pb.persona.agent_name}, {pb.persona.role_title} ({pb.persona.tone})\n"
        f"Stages: {', '.join(s.name for s in pb.stages)}\n"
        f"Objections covered: {len(pb.objections)}   Key terms: {len(pb.keyterms)}\n"
        f"Capabilities: {', '.join(c.key + ('*' if c.required else '') for c in pb.required_capabilities) or '-'}\n"
        f"Readiness: [bold]{pb.readiness_score}/100[/bold] - {pb.readiness_notes}",
        title="Playbook",
    ))


@app.command()
def onboard(
    script: Path = typer.Argument(..., exists=True, readable=True, help="The business's script / pitch / notes (text or markdown)."),
    name: str = typer.Option(..., "--name", "-n", help="Campaign name."),
    non_interactive: bool = typer.Option(False, help="Do not ask questions; accept suggested defaults."),
    max_rounds: int = typer.Option(3, help="Max Q&A rounds."),
):
    """Learn the pitch from a script, ask what's missing, and save the playbook."""
    from .playbook.analyzer import PlaybookAnalyzer

    text = script.read_text(encoding="utf-8")
    analyzer = PlaybookAnalyzer()
    with console.status("Reading the script and building the playbook..."):
        pb = analyzer.analyze(text)
    _print_playbook_summary(pb)

    answers: list[QAPair] = []
    rounds = 0
    while pb.open_questions and rounds < max_rounds:
        rounds += 1
        console.print(f"\n[bold]The agent has {len(pb.open_questions)} question(s) about your pitch[/bold] "
                      f"({len(pb.blocking_questions)} blocking):")
        new_answers: list[QAPair] = []
        for i, q in enumerate(pb.open_questions, 1):
            tag = "[red]blocking[/red]" if q.blocking else "[yellow]optional[/yellow]"
            console.print(f"\n{i}. {q.question}  {tag}\n   [dim]{q.why_it_matters}[/dim]")
            if non_interactive:
                if q.suggested_default:
                    new_answers.append(QAPair(question=q.question, answer=f"(default accepted) {q.suggested_default}"))
                continue
            default = q.suggested_default or None
            ans = Prompt.ask("   Your answer", default=default) if default else Prompt.ask("   Your answer (Enter to skip)", default="")
            if ans.strip():
                new_answers.append(QAPair(question=q.question, answer=ans.strip()))
        if not new_answers:
            break
        answers.extend(new_answers)
        with console.status("Folding your answers into the playbook..."):
            pb = analyzer.analyze(text, answers=answers, previous=pb)
        _print_playbook_summary(pb)

    c = _save_campaign(name, pb, text)
    console.print(f"\nSaved playbook to [green]{_playbook_path(name)}[/green] and campaign [bold]{c.name}[/bold] (status: {c.status}).")
    if pb.blocking_questions:
        console.print("[yellow]There are still blocking questions. Re-run onboard, or start with --force to dial anyway.[/yellow]")
    else:
        console.print("Next: [cyan]callagent rehearse[/cyan] to let the agent practice, then [cyan]callagent leads import[/cyan].")


@app.command()
def rehearse(
    name: str = typer.Argument(..., help="Campaign name."),
    persona: list[str] = typer.Option(None, "--persona", "-p", help="Prospect persona(s) to rehearse against."),
    turns: int = typer.Option(12, help="Max turns per rehearsal."),
    apply: bool = typer.Option(False, help="Apply the coach's playbook edits automatically."),
):
    """Let the agent practice the pitch against simulated prospects and get coached."""
    from .rehearsal import apply_coach_edits, run_rehearsal
    from .llm import make_client

    pb, script_text = _load_playbook(name)
    settings = get_settings()
    client = make_client(settings)

    def show(t):
        console.rule(f"[dim]{t.persona}[/dim]")
        for turn in t.turns:
            who = f"[cyan]{pb.persona.agent_name}[/cyan]" if turn["role"] == "agent" else "[magenta]Prospect[/magenta]"
            console.print(f"{who}: {turn['text']}")
        if t.tool_calls:
            console.print("[dim]tools: " + ", ".join(c["tool"] for c in t.tool_calls) + "[/dim]")

    with console.status("Rehearsing..."):
        transcripts, report = run_rehearsal(pb, persona or None, turns, settings, client, on_transcript=show)

    console.rule("Coach report")
    console.print(f"Score: [bold]{report.overall_score}/100[/bold]   Ready for real calls: {'[green]yes[/green]' if report.ready_for_real_calls else '[red]no[/red]'}")
    console.print(report.readiness_notes)
    for title, items in (("Strengths", report.strengths), ("Weaknesses", report.weaknesses),
                         ("Sounded robotic", report.sounded_robotic_examples), ("Tool discipline", report.tool_discipline_issues),
                         ("Suggested playbook edits", report.playbook_edits)):
        if items:
            console.print(f"\n[bold]{title}[/bold]")
            for it in items:
                console.print(f" - {it}")

    settings.data_dir.joinpath("rehearsals").mkdir(parents=True, exist_ok=True)
    out = settings.data_dir / "rehearsals" / f"{_playbook_path(name).stem}-{len(list(settings.data_dir.joinpath('rehearsals').glob('*.json'))) + 1}.json"
    out.write_text(json.dumps({"transcripts": [t.__dict__ for t in transcripts], "report": report.model_dump()}, indent=2, default=str))
    console.print(f"\nSaved to {out}")

    if report.playbook_edits and (apply or Confirm.ask("Apply these edits to the playbook?", default=False)):
        with console.status("Applying edits..."):
            pb = apply_coach_edits(client, pb, report, settings)
        _save_campaign(name, pb, script_text)
        console.print("[green]Playbook updated.[/green] Run rehearse again to verify.")


@app.command()
def chat(
    name: str = typer.Argument(..., help="Campaign name."),
    model: str = typer.Option(None, help="Override CALL_MODEL for this session (e.g. claude-sonnet-5)."),
    thinking: str = typer.Option(None, help="Override CALL_THINKING: disabled | adaptive."),
    fast: bool = typer.Option(False, help="Use fast mode for this session."),
):
    """Talk to the agent in the terminal with the live-call model settings. Shows time-to-first-word per turn."""
    from .chat import ChatSession

    pb, _ = _load_playbook(name)
    settings = get_settings().model_copy(update={k: v for k, v in {"call_model": model, "call_thinking": thinking, "call_fast_mode": fast or None}.items() if v})
    session = ChatSession(pb, settings)
    console.print(Panel.fit(
        f"model [bold]{settings.call_model}[/bold] · thinking {settings.call_thinking} · effort {settings.call_effort}"
        f"{' · fast mode' if settings.call_fast_mode else ''}\nYou are the prospect. Type what you'd say on the phone. Ctrl-C to stop.",
        title=f"Chat with {pb.persona.agent_name}"))
    user_text = "Hello?"
    console.print(f"[magenta]You[/magenta]: {user_text}")
    try:
        while not session.ended:
            console.print(f"[cyan]{pb.persona.agent_name}[/cyan]: ", end="")
            reply, st = session.say(user_text, on_text=lambda t: console.print(t, end="", highlight=False))
            console.print()
            tools = f"  tools: {', '.join(st.tools)}" if st.tools else ""
            console.print(f"[dim]  first word {st.ttft_secs:.2f}s · full reply {st.total_secs:.2f}s · {st.output_tokens} tokens{tools}[/dim]")
            if session.ended:
                break
            user_text = Prompt.ask("[magenta]You[/magenta]")
    except KeyboardInterrupt:
        pass
    if session.stats:
        ttfts = sorted(s.ttft_secs for s in session.stats)
        console.print(f"\n[bold]Model latency this session[/bold]: first word p50 {ttfts[len(ttfts)//2]:.2f}s, max {ttfts[-1]:.2f}s over {len(ttfts)} turns. "
                      "On the phone add roughly 0.3s for speech recognition and 0.2s for the voice.")


@playbook_app.command("show")
def playbook_show(name: str, full: bool = typer.Option(False, help="Print the whole JSON.")):
    pb, _ = _load_playbook(name)
    if full:
        console.print_json(pb.model_dump_json())
    else:
        _print_playbook_summary(pb)
        if pb.open_questions:
            console.print("\n[bold]Open questions[/bold]")
            for q in pb.open_questions:
                console.print(f" - {'[red]*[/red] ' if q.blocking else ''}{q.question}")


@playbook_app.command("prompt")
def playbook_prompt(name: str, voicemail: bool = False):
    """Print the live-call system prompt the agent will run with (for review)."""
    from .playbook.prompt_builder import build_system_prompt

    pb, _ = _load_playbook(name)
    lead = {"first_name": "Sam", "last_name": "Taylor", "company": "Taylor & Co", "timezone": get_settings().default_timezone}
    console.print(build_system_prompt(pb, lead, mode="voicemail" if voicemail else "live", callback_number=get_settings().twilio_from_number or ""))


@playbook_app.command("import")
def playbook_import(name: str, path: Path = typer.Argument(..., exists=True)):
    """Load a hand-edited playbook JSON into a campaign."""
    pb = Playbook.model_validate_json(path.read_text())
    _save_campaign(name, pb, "")
    console.print(f"[green]Imported[/green] playbook for {name}.")


@leads_app.command("import")
def leads_import(
    name: str = typer.Argument(..., help="Campaign name."),
    source: str = typer.Argument(..., help="CSV / XLSX path, or a Google Sheets link shared with 'anyone with the link'."),
):
    """Import a lead sheet. Needs a phone column; name/company/email/timezone are picked up if present."""
    from .leads.importer import import_leads

    settings = get_settings()
    with session_scope() as s:
        c = get_campaign_by_name(s, name)
        if c is None:
            raise typer.BadParameter(f"campaign '{name}' not found")
        report = import_leads(s, c, source, settings.default_phone_region)
    t = Table(title="Import report")
    t.add_column("metric"); t.add_column("value")
    for k, v in report.as_dict().items():
        t.add_row(k, json.dumps(v) if isinstance(v, (dict, list)) else str(v))
    console.print(t)


@app.command()
def serve(host: str = typer.Option(None), port: int = typer.Option(None), reload: bool = False):
    """Run the webhook + media-stream server (expose it publicly, e.g. `ngrok http 8000`)."""
    import uvicorn

    settings = get_settings()
    missing = settings.missing_for_calls()
    if missing:
        console.print(f"[yellow]Missing for live calls: {', '.join(missing)}[/yellow]")
    uvicorn.run("callagent.server:app", host=host or settings.server_host, port=port or settings.server_port, reload=reload)


def _api(method: str, path: str, **kwargs):
    settings = get_settings()
    headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {resolve_api_token(settings)}"}
    try:
        r = httpx.request(method, settings.server_url + path, timeout=60, headers=headers, **kwargs)
    except httpx.ConnectError:
        raise typer.Exit(console.print(f"[red]Cannot reach the server at {settings.server_url}. Start it with `callagent serve`.[/red]") or 1)
    if r.status_code >= 400:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
        console.print(f"[red]{r.status_code}: {detail}[/red]")
        raise typer.Exit(1)
    return r


def _print_campaign(c: dict) -> None:
    t = Table(title=f"{c['name']}  (status: {c['status']}, dialer {'running' if c['running'] else 'stopped'}, readiness {c['readiness_score']})")
    t.add_column("metric"); t.add_column("count", justify="right")
    for k, v in sorted(c["stats"].items()):
        t.add_row(k, str(v))
    console.print(t)


@campaign_app.command("start")
def campaign_start(name: str, concurrency: int = typer.Option(None), force: bool = typer.Option(False, help="Dial even with blocking open questions.")):
    """Start dialing the campaign's leads."""
    params = {"force": str(force).lower()}
    if concurrency:
        params["concurrency"] = str(concurrency)
    _print_campaign(_api("POST", f"/api/campaigns/{name}/start", params=params).json())


@campaign_app.command("pause")
def campaign_pause(name: str):
    """Stop dialing (calls in progress finish normally)."""
    _print_campaign(_api("POST", f"/api/campaigns/{name}/pause").json())


@campaign_app.command("status")
def campaign_status(name: str = typer.Argument(None)):
    """Lead counts by status and dispositions, for one campaign or all."""
    if name:
        _print_campaign(_api("GET", f"/api/campaigns/{name}").json())
    else:
        for c in _api("GET", "/api/campaigns").json():
            _print_campaign(c)


@campaign_app.command("calls")
def campaign_calls(name: str, limit: int = 20):
    """Recent calls with disposition and summary."""
    rows = _api("GET", f"/api/campaigns/{name}/calls", params={"limit": limit}).json()
    t = Table(title="Recent calls")
    for col in ("call_run_id", "lead", "phone", "status", "mode", "disposition", "duration_seconds", "summary"):
        t.add_column(col)
    for r in rows:
        t.add_row(*(str(r.get(col) or "") for col in ("call_run_id", "lead", "phone", "status", "mode", "disposition", "duration_seconds", "summary")))
    console.print(t)


@campaign_app.command("export")
def campaign_export(name: str, out: Path = typer.Argument(Path("results.csv"))):
    """Export every lead with status, disposition, summary, next action and meeting time."""
    out.write_bytes(_api("GET", f"/api/campaigns/{name}/export.csv").content)
    console.print(f"[green]Wrote {out}[/green]")


@app.command("test-call")
def test_call(name: str, phone: str, first_name: str = ""):
    """Place one real call to a number of yours to hear the agent."""
    r = _api("POST", f"/api/campaigns/{name}/dial", params={"phone": phone, "first_name": first_name}).json()
    console.print(f"Dialing... call_run_id={r['call_run_id']}. Watch it with: callagent campaign calls \"{name}\"")


@app.command()
def doctor():
    """Check configuration."""
    settings = get_settings()
    t = Table(title="Configuration")
    t.add_column("setting"); t.add_column("value")
    t.add_row("onboarding model", settings.onboarding_model + f" (effort {settings.onboarding_effort})")
    t.add_row("call model", settings.call_model + f" (effort {settings.call_effort})")
    t.add_row("STT", f"Deepgram {settings.deepgram_model}")
    t.add_row("TTS", f"ElevenLabs {settings.elevenlabs_model} voice={settings.elevenlabs_voice_id or '-'}")
    t.add_row("from number", settings.twilio_from_number or "-")
    t.add_row("public URL", settings.public_base_url or "-")
    t.add_row("calling window", f"{settings.calling_window_start:%H:%M}-{settings.calling_window_end:%H:%M} {settings.default_timezone}")
    t.add_row("database", settings.database_url)
    console.print(t)
    missing = settings.missing_for_calls()
    console.print("[green]Ready for live calls.[/green]" if not missing else f"[yellow]Missing for live calls: {', '.join(missing)}[/yellow]")


if __name__ == "__main__":
    app()
